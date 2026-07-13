"""Dispatch: run approved plan items through the governor to their owning agents.

Every item passes two governor gates before anything happens:
  1. :meth:`Governor.check_action` — kill switch, approval, and the effective
     dry-run (an approved item runs live only if the human chose ``go_live``).
  2. :meth:`Governor.check_budget` — per-run/per-day caps (live items only).
A refused item is marked ``failed`` with the reason and never executes. On a live
run the action's actual cost is charged to the ledger. ``notify`` is handled by
the orchestrator itself; every other action routes to its owning agent's
``execute`` (implemented in Phase 4).
"""

from __future__ import annotations

from ..agents import build_agent
from ..context import RunContext
from ..db.enums import EntryType, PlanItemStatus, PlanStatus
from ..interfaces import ActionResult, EntryDraft, PlanItemView
from ..logging_ import get_logger
from .plans import PlanRepo, to_view
from .slack import post_brief

log = get_logger("orchestrator.dispatch")

_AGENT_NAMES = {"research", "analytics", "opportunity", "production", "paid_media", "reporting"}


class DispatchError(RuntimeError):
    pass


class Dispatcher:
    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.repo = PlanRepo(ctx.session)

    async def dispatch_plan(self, plan_id: int) -> dict:
        plan = await self.repo.get_plan(plan_id)
        if plan is None:
            raise DispatchError(f"plan {plan_id} not found")
        if plan.status not in (PlanStatus.APPROVED.value, PlanStatus.DISPATCHED.value):
            raise DispatchError(f"plan {plan_id} is '{plan.status}', not approved — cannot dispatch")

        summary = {"plan_id": plan_id, "dispatched": 0, "done": 0, "failed": 0, "refused": 0, "items": []}
        for item in await self.repo.approved_items(plan_id):
            view = to_view(item, plan.brand)
            decision = self.ctx.governor.check_action(view)
            if not decision.allowed:
                await self.repo.mark_item(item.id, PlanItemStatus.FAILED.value, {"refused": decision.reason})
                summary["refused"] += 1
                summary["items"].append({"id": item.id, "action": item.action_type, "result": "refused",
                                         "reason": decision.reason})
                continue

            effective = view.model_copy(update={"dry_run": decision.dry_run})
            if not decision.dry_run:
                budget = await self.ctx.governor.check_budget(effective)
                if not budget.allowed:
                    await self.repo.mark_item(item.id, PlanItemStatus.FAILED.value, {"refused": budget.reason})
                    summary["refused"] += 1
                    summary["items"].append({"id": item.id, "action": item.action_type,
                                             "result": "refused", "reason": budget.reason})
                    continue

            await self.repo.mark_item(item.id, PlanItemStatus.DISPATCHED.value)
            summary["dispatched"] += 1
            result = await self._run(effective)

            if result.entries:
                await self.ctx.store.write_many(result.entries)
            if not effective.dry_run and result.ok:
                await self._charge(result, effective.assigned_agent)

            status = PlanItemStatus.DONE.value if result.ok else PlanItemStatus.FAILED.value
            await self.repo.mark_item(item.id, status, {**result.result_ref, "dry_run": result.dry_run,
                                                        "summary": result.summary, "error": result.error})
            summary["done" if result.ok else "failed"] += 1
            summary["items"].append({"id": item.id, "action": item.action_type,
                                     "result": status, "dry_run": result.dry_run, "summary": result.summary})

        await self.repo.set_plan_status(plan_id, PlanStatus.DISPATCHED.value)
        log.info("dispatched plan %s: %s", plan_id, {k: summary[k] for k in ("done", "failed", "refused")})
        return summary

    async def _run(self, view: PlanItemView) -> ActionResult:
        if view.action_type == "notify":
            posted = await post_brief(self.ctx, view.brand, str(view.params.get("message", "")))
            entry = EntryDraft(
                type=EntryType.DECISION, brand=view.brand, source_agent="orchestrator",
                source_system="orchestrator",
                payload={"kind": "notify", "message": view.params.get("message"), "posted": posted},
            )
            return ActionResult(ok=True, dry_run=view.dry_run, action_type="notify",
                                summary="notified" if posted else "logged", result_ref={"posted": posted},
                                entries=[entry])
        if view.assigned_agent not in _AGENT_NAMES:
            return ActionResult(ok=False, dry_run=view.dry_run, action_type=view.action_type,
                                error=f"no agent '{view.assigned_agent}' to execute action")
        agent = build_agent(view.assigned_agent, self.ctx)
        try:
            return await agent.execute(view)
        except NotImplementedError:
            return ActionResult(ok=False, dry_run=view.dry_run, action_type=view.action_type,
                                error="action adapter not implemented yet (Phase 4)")

    async def _charge(self, result: ActionResult, agent: str) -> None:
        for metric in ("ahrefs_units", "llm_micros", "bq_bytes"):
            amount = int(getattr(result.cost, metric, 0) or 0)
            if amount > 0:
                await self.ctx.governor.charge(metric, amount, agent)
