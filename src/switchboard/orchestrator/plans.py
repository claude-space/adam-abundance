"""Plan / plan_item persistence + the human-approval gate.

The governor's approval rule lives partly here: :meth:`approve_item` and
:meth:`approve_plan` refuse to record ``orchestrator`` (or an empty actor) as the
approver — the orchestrator cannot self-approve (PRD §8). Going *live* is an
explicit, separate choice (``go_live=True``) that flips ``dry_run`` off; the
governor still budget-checks it at dispatch.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db.enums import PlanItemStatus, PlanStatus
from ..db.models import Plan, PlanItem
from ..interfaces import PlanItemView
from ..logging_ import get_logger

log = get_logger("orchestrator.plans")


class ApprovalError(RuntimeError):
    """Raised on an illegitimate approval (self-approval or missing actor)."""


def to_view(item: PlanItem, brand: str) -> PlanItemView:
    return PlanItemView(
        id=item.id, plan_id=item.plan_id, rank=item.rank, assigned_agent=item.assigned_agent,
        action_type=item.action_type, params=item.params or {}, rationale=item.rationale,
        status=item.status, dry_run=item.dry_run, brand=brand, cost_estimate=item.cost_estimate,
    )


class PlanRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_plan(self, brand: str, plan_date: date, created_by: str = "orchestrator") -> Plan:
        plan = Plan(plan_date=plan_date, brand=brand, status=PlanStatus.DRAFT.value, created_by=created_by)
        self.session.add(plan)
        await self.session.flush()
        return plan

    async def add_item(
        self, plan: Plan, *, rank: int, assigned_agent: str, action_type: str,
        params: dict[str, Any], rationale: str | None = None,
        cost_estimate: dict[str, Any] | None = None, dry_run: bool = True,
    ) -> PlanItem:
        item = PlanItem(
            plan_id=plan.id, rank=rank, assigned_agent=assigned_agent, action_type=action_type,
            params=params, rationale=rationale, cost_estimate=cost_estimate, dry_run=dry_run,
            status=PlanItemStatus.PROPOSED.value,
        )
        self.session.add(item)
        await self.session.flush()
        return item

    async def get_plan(self, plan_id: int) -> Plan | None:
        result = await self.session.execute(
            select(Plan).where(Plan.id == plan_id).options(selectinload(Plan.items))
        )
        return result.scalar_one_or_none()

    async def latest_plan(self, brand: str, plan_date: date | None = None) -> Plan | None:
        stmt = select(Plan).where(Plan.brand == brand).options(selectinload(Plan.items))
        if plan_date is not None:
            stmt = stmt.where(Plan.plan_date == plan_date)
        stmt = stmt.order_by(Plan.created_at.desc()).limit(1)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_plans(self, limit: int = 30) -> list[Plan]:
        result = await self.session.execute(
            select(Plan).options(selectinload(Plan.items)).order_by(Plan.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def _get_item(self, item_id: int) -> PlanItem:
        item = (await self.session.execute(select(PlanItem).where(PlanItem.id == item_id))).scalar_one_or_none()
        if item is None:
            raise ApprovalError(f"plan_item {item_id} not found")
        return item

    @staticmethod
    def _validate_actor(approver: str) -> None:
        if not approver or approver.strip().lower() in ("", "orchestrator", "system"):
            raise ApprovalError("A human approver is required; the orchestrator cannot self-approve.")

    async def approve_item(self, item_id: int, approver: str, *, go_live: bool = False) -> PlanItem:
        self._validate_actor(approver)
        item = await self._get_item(item_id)
        item.status = PlanItemStatus.APPROVED.value
        item.dry_run = not go_live  # live requires an explicit human choice
        await self.session.flush()
        log.info("plan_item %s approved by %s (go_live=%s)", item_id, approver, go_live)
        return item

    async def reject_item(self, item_id: int, approver: str) -> PlanItem:
        item = await self._get_item(item_id)
        item.status = PlanItemStatus.REJECTED.value
        await self.session.flush()
        log.info("plan_item %s rejected by %s", item_id, approver)
        return item

    async def edit_item(self, item_id: int, *, params: dict[str, Any] | None = None,
                        rationale: str | None = None) -> PlanItem:
        item = await self._get_item(item_id)
        if params is not None:
            item.params = params
        if rationale is not None:
            item.rationale = rationale
        await self.session.flush()
        return item

    async def approve_plan(self, plan_id: int, approver: str) -> Plan:
        self._validate_actor(approver)
        plan = await self.get_plan(plan_id)
        if plan is None:
            raise ApprovalError(f"plan {plan_id} not found")
        plan.status = PlanStatus.APPROVED.value
        plan.approved_by = approver
        plan.approved_at = datetime.now(timezone.utc)
        await self.session.flush()
        log.info("plan %s approved by %s", plan_id, approver)
        return plan

    async def mark_item(self, item_id: int, status: str, result_ref: dict[str, Any] | None = None) -> PlanItem:
        item = await self._get_item(item_id)
        item.status = status
        if result_ref is not None:
            item.result_ref = result_ref
        await self.session.flush()
        return item

    async def set_plan_status(self, plan_id: int, status: str) -> None:
        plan = await self.get_plan(plan_id)
        if plan:
            plan.status = status
            await self.session.flush()

    async def approved_items(self, plan_id: int) -> Sequence[PlanItem]:
        result = await self.session.execute(
            select(PlanItem).where(
                PlanItem.plan_id == plan_id,
                PlanItem.status == PlanItemStatus.APPROVED.value,
            ).order_by(PlanItem.rank)
        )
        return list(result.scalars().all())
