"""Base class for tool adapters.

Subclasses implement the async ``_observe`` (read) and/or ``_act`` (side effect)
hooks; the base wraps each in a ``tool_call_log`` row (with redacted request),
error isolation (a failing adapter degrades to an empty result + a logged
failure, never crashing an agent's observe pass), and — for actions — the
dry-run contract.

Read adapters MUST NOT override ``_act`` at all: an adapter with no ``_act`` is
*structurally* read-only, which is how the Paid-Media adapters prove they can
never mutate an ad platform (PRD §8, Phase 1 acceptance).
"""

from __future__ import annotations

from typing import Any

from ..context import RunContext
from ..db.enums import ToolAction
from ..interfaces import ActionResult, CostSpec, EntryDraft, PlanItemView
from ..logging_ import get_logger

log = get_logger("adapter")


class AdapterUnavailable(RuntimeError):
    """Raised when an adapter can't run (missing credential, absent optional SDK,
    endpoint unreachable). Treated as a soft failure: logged, empty result."""


class BaseAdapter:
    #: Stable adapter name used in tool_call_log.tool and agent.owned_tools.
    name: str = "base"
    #: Value written to memory_entry.source_system for entries this adapter feeds.
    source_system: str | None = None
    #: The worker agent that owns this adapter (for the audit trail).
    owner_agent: str = "system"

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    # -- read -----------------------------------------------------------------

    async def observe(self, brand: str, **kwargs: Any) -> list[EntryDraft]:
        """Read from the underlying system and return typed entries. Logs one
        'read' tool_call_log row. Never performs a side effect."""
        request = {"brand": brand, **{k: v for k, v in kwargs.items() if _loggable(v)}}
        try:
            drafts, cost = await self._observe(brand, **kwargs)
        except AdapterUnavailable as exc:
            log.info("[%s] unavailable for %s: %s", self.name, brand, exc)
            await self.ctx.store.log_tool_call(
                agent=self.owner_agent, tool=self.name, action=ToolAction.READ.value,
                dry_run=True, brand=brand, request={**request, "unavailable": str(exc)}, ok=False,
            )
            return []
        except Exception as exc:  # noqa: BLE001 — isolate adapter failures
            log.exception("[%s] observe failed for %s", self.name, brand)
            await self.ctx.store.log_tool_call(
                agent=self.owner_agent, tool=self.name, action=ToolAction.READ.value,
                dry_run=True, brand=brand, request={**request, "error": str(exc)}, ok=False,
            )
            return []
        await self.ctx.store.log_tool_call(
            agent=self.owner_agent, tool=self.name, action=ToolAction.READ.value,
            dry_run=False, brand=brand, request=request, ok=True, cost=cost,
        )
        # Metered reads (Ahrefs units, BQ bytes, any LLM used in a read) count
        # against the governor's caps just like actions do (PRD §8).
        await self._charge_cost(cost)
        return drafts

    async def _charge_cost(self, cost: CostSpec) -> None:
        for metric in ("ahrefs_units", "llm_micros", "bq_bytes"):
            amount = int(getattr(cost, metric, 0) or 0)
            if amount > 0:
                await self.ctx.governor.charge(metric, amount, self.owner_agent)

    async def _observe(self, brand: str, **kwargs: Any) -> tuple[list[EntryDraft], CostSpec]:
        """Override to perform the read. Return (drafts, cost)."""
        raise NotImplementedError(f"{self.name} has no read surface")

    # -- act (Phase 4) --------------------------------------------------------

    @property
    def can_act(self) -> bool:
        """True only if this adapter defines its own ``_act`` (i.e. is not a
        structurally read-only adapter)."""
        return type(self)._act is not BaseAdapter._act

    async def act(self, item: PlanItemView, *, dry_run: bool) -> ActionResult:
        """Perform a governor-gated side effect for an approved plan_item. The
        caller (dispatcher) is responsible for the governor check; this method
        enforces the dry-run contract and logs an 'act' row."""
        if not self.can_act:
            raise AdapterUnavailable(f"{self.name} is read-only; no action surface")
        request = {"action_type": item.action_type, "params": item.params, "plan_item_id": item.id}
        try:
            result = await self._act(item, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] act failed for %s", self.name, item.action_type)
            await self.ctx.store.log_tool_call(
                agent=self.owner_agent, tool=self.name, action=ToolAction.ACT.value,
                dry_run=dry_run, brand=item.brand, request={**request, "error": str(exc)}, ok=False,
            )
            return ActionResult(
                ok=False, dry_run=dry_run, action_type=item.action_type, error=str(exc)
            )
        await self.ctx.store.log_tool_call(
            agent=self.owner_agent, tool=self.name, action=ToolAction.ACT.value,
            dry_run=result.dry_run, brand=item.brand, request=request, ok=result.ok, cost=result.cost,
        )
        return result

    async def _act(self, item: PlanItemView, *, dry_run: bool) -> ActionResult:
        """Override in action adapters. MUST honor dry_run (log-only when true)."""
        raise NotImplementedError(f"{self.name} has no action surface")


def _loggable(value: Any) -> bool:
    """Keep tool_call_log requests small: drop bulky/opaque kwargs."""
    return isinstance(value, (str, int, float, bool, type(None)))
