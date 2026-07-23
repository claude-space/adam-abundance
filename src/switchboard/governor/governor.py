"""Governor implementation.

Two decision surfaces:

* :meth:`check_action` (sync, no DB) — kill switch, the human-approval gate, and
  dry-run forcing. An item may only run *live* (``dry_run=False``) if it is
  approved; the orchestrator can never self-approve, so an unapproved item is
  refused for live and (optionally) allowed as a log-only dry run.
* :meth:`check_budget` (async, DB) — per-run and per-day hard caps for the three
  metered resources. When a cap would be exceeded the action is *refused* (not
  silently queued) and a ``flag`` is written to memory.

Spend is recorded in ``spend_ledger`` via :meth:`charge`; caps come from config
(:class:`~switchboard.config.SpendCaps`) — config, not code.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, SpendCaps, get_settings
from ..db.enums import EntryType, PlanItemStatus
from ..db.models import SpendLedger
from ..interfaces import Decision, EntryDraft, PlanItemView
from ..logging_ import get_logger
from ..memory.store import MemoryStore
from .caps_config import resolve_caps

log = get_logger("governor")

_LIVE_OK_STATUSES = {
    PlanItemStatus.APPROVED.value,
    PlanItemStatus.DISPATCHED.value,
    PlanItemStatus.RUNNING.value,
}
_METRICS = ("ahrefs_units", "llm_micros", "bq_bytes")


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


class Governor:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.store = MemoryStore(session)
        self._eff_caps: SpendCaps | None = None

    async def _caps(self) -> SpendCaps:
        """Effective caps = shipped defaults overlaid with the admin's runtime
        override (Governor page). Resolved once per governor instance (= once per
        operation) so a config change is picked up on the next action."""
        if self._eff_caps is None:
            self._eff_caps = await resolve_caps(self.session, self.settings.caps)
        return self._eff_caps

    # -- sync policy: kill switch + approval gate + dry-run forcing -----------

    def check_action(self, item: PlanItemView) -> Decision:
        violations: list[str] = []

        # Kill switch halts all dispatch + live actions; observe keeps running
        # elsewhere (PRD §8).
        if self.settings.kill_switch:
            return Decision(
                allowed=False,
                dry_run=True,
                reason="Kill switch engaged: all dispatch and live actions halted.",
                violations=["kill_switch"],
            )

        wants_live = item.dry_run is False
        approved = item.status in _LIVE_OK_STATUSES

        if wants_live and not approved:
            # A live side effect on an unapproved item is a governance breach.
            return Decision(
                allowed=False,
                dry_run=True,
                reason=(
                    f"Live action requires an approved plan_item; status is "
                    f"'{item.status}'. Refusing live; no side effect performed."
                ),
                violations=["approval_gate"],
            )

        # Effective dry_run: live only when explicitly requested AND approved.
        effective_dry_run = not (wants_live and approved)
        if not effective_dry_run:
            violations = []  # cleared; budget still checked separately
        return Decision(
            allowed=True,
            dry_run=effective_dry_run,
            reason="Approved for live action." if not effective_dry_run else "Dry-run (log-only).",
            violations=violations,
        )

    # -- async budget caps ----------------------------------------------------

    async def spent_today(self, metric: str) -> int:
        result = await self.session.execute(
            select(func.coalesce(func.sum(SpendLedger.amount), 0)).where(
                SpendLedger.window_date == _utc_today(),
                SpendLedger.metric == metric,
            )
        )
        return int(result.scalar_one())

    async def remaining(self, metric: str) -> int | None:
        cap = (await self._caps()).per_day(metric)
        if cap is None:
            return None
        return max(0, cap - await self.spent_today(metric))

    async def within_caps(self, metric: str, *, additional: int = 0) -> bool:
        cap = (await self._caps()).per_day(metric)
        if cap is None:
            return True
        return (await self.spent_today(metric)) + additional <= cap

    async def check_budget(self, item: PlanItemView) -> Decision:
        """Enforce per-run and per-day caps against ``item.cost_estimate``.
        Writes a cap-hit flag and refuses when a cap would be exceeded."""
        est = item.cost_estimate or {}
        violations: list[str] = []
        for metric in _METRICS:
            amount = int(est.get(metric, 0) or 0)
            if amount <= 0:
                continue
            per_run = (await self._caps()).per_run(metric)
            if per_run is not None and amount > per_run:
                violations.append(f"{metric}:per_run({amount}>{per_run})")
                await self._write_cap_flag(item, metric, amount, per_run, scope="per_run")
                continue
            if not await self.within_caps(metric, additional=amount):
                cap = (await self._caps()).per_day(metric)
                spent = await self.spent_today(metric)
                violations.append(f"{metric}:per_day({spent}+{amount}>{cap})")
                await self._write_cap_flag(item, metric, spent + amount, cap or 0, scope="per_day")

        if violations:
            return Decision(
                allowed=False,
                dry_run=True,
                reason=f"Spend cap(s) exceeded: {', '.join(violations)}. Action refused.",
                violations=violations,
            )
        return Decision(allowed=True, dry_run=item.dry_run, reason="Within caps.")

    async def charge(self, metric: str, amount: int, agent: str) -> None:
        if amount <= 0:
            return
        self.session.add(
            SpendLedger(window_date=_utc_today(), metric=metric, amount=int(amount), agent=agent)
        )
        await self.session.flush()
        log.info("Charged %s %d to %s (spent today=%d)", metric, amount, agent, await self.spent_today(metric))

    # -- helpers --------------------------------------------------------------

    async def _write_cap_flag(
        self, item: PlanItemView, metric: str, would_be: int, cap: int, *, scope: str
    ) -> None:
        await self.store.write(
            EntryDraft(
                type=EntryType.FLAG,
                brand=item.brand,
                source_agent="governor",
                source_system="governor",
                payload={
                    "kind": "spend_cap_exceeded",
                    "metric": metric,
                    "scope": scope,
                    "would_be": would_be,
                    "cap": cap,
                    "action_type": item.action_type,
                    "plan_item_id": item.id,
                    "severity": "high",
                },
                confidence=1.0,
            )
        )
        log.warning(
            "Spend cap hit (%s %s): would be %d vs cap %d — refusing action %s",
            metric,
            scope,
            would_be,
            cap,
            item.action_type,
        )
