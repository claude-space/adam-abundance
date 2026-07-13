"""Base worker agent.

The default ``observe`` runs each owned adapter and persists its drafts to shared
memory. Persistence goes through the store with ``fact_gate_ok=False``, so any
agent that *tries* to write a verified fact has it downgraded to a claim — only
the Research agent (which overrides this) may certify facts (PRD §6.2, §8).
"""

from __future__ import annotations

from ..adapters.registry import build_action_adapter, build_adapters, owned_tool_names
from ..context import RunContext
from ..interfaces import ActionResult, EntryDraft, PlanItemView
from ..logging_ import get_logger

log = get_logger("agent")


class BaseAgent:
    name: str = "base"

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.adapters = build_adapters(self.name, ctx)

    @property
    def owned_tools(self) -> list[str]:
        return owned_tool_names(self.name)

    async def observe(self, brand: str) -> int:
        """Run each owned adapter and persist findings. Returns entries written."""
        written = 0
        for adapter in self.adapters:
            drafts = await adapter.observe(brand)
            if drafts:
                persisted = await self._persist(drafts)
                written += len(persisted)
        log.info("[%s] observe(%s) wrote %d entries", self.name, brand, written)
        return written

    async def _persist(self, drafts: list[EntryDraft]):
        # Default path: never certifies facts (fact_gate_ok=False).
        return await self.ctx.store.write_many(drafts, fact_gate_ok=False)

    async def execute(self, item: PlanItemView) -> ActionResult:
        """Run one approved plan_item via the owning action adapter (Phase 4).

        The governor (in dispatch) has already vetted approval + budget and set
        the effective ``item.dry_run``. Here we enforce domain ownership: an agent
        may only execute an action whose adapter it owns — it cannot reach into
        another domain's action (PRD §5, §14)."""
        adapter = build_action_adapter(item.action_type, self.ctx)
        if adapter is None:
            return ActionResult(ok=False, dry_run=item.dry_run, action_type=item.action_type,
                                error=f"no action adapter for '{item.action_type}'")
        if adapter.owner_agent != self.name:
            return ActionResult(ok=False, dry_run=item.dry_run, action_type=item.action_type,
                                error=f"ownership violation: {self.name} may not run "
                                      f"{item.action_type} (owned by {adapter.owner_agent})")
        return await adapter.act(item, dry_run=item.dry_run)
