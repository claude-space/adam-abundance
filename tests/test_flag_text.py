"""Tests for flag descriptions (orchestrator/flag_text.py): the shared
describe_flag() used by the planner + backfill, and the backfill that rewrites
existing vague plan-item descriptions."""

from __future__ import annotations

from datetime import date

import pytest

from switchboard.orchestrator.flag_text import backfill_flag_descriptions, describe_flag


# --- describe_flag --------------------------------------------------------

def test_writer_below_index_names_the_writer_and_index():
    title, rationale = describe_flag(
        {"kind": "writer_below_index", "writer": "B. Writer", "relative_index": 0.56, "articles": 9})
    assert title == "Underperforming writer: B. Writer (56% of the cohort average · 9 articles)"
    assert "B. Writer" in rationale and "1.0 = cohort average" in rationale
    assert "writer_below_index" not in title


def test_unknown_kind_is_humanized_with_identifier():
    title, rationale = describe_flag({"kind": "some_new_thing", "url": "hotcars.com/x"})
    assert title == "Some new thing — hotcars.com/x"
    assert 'Flag “some new thing” surfaced' in rationale


def test_unknown_kind_without_identifier():
    title, _ = describe_flag({"kind": "mystery"})
    assert title == "Mystery"


def test_empty_payload_is_safe():
    title, rationale = describe_flag({})
    assert title == "Flag" and "surfaced" in rationale
    assert describe_flag(None)[0] == "Flag"


# --- backfill (real DB) ---------------------------------------------------

async def test_backfill_rewrites_vague_flag_items():
    from sqlalchemy import delete
    from switchboard.context import RunContext
    from switchboard.db.models import Plan, PlanItem

    try:
        async with RunContext.open() as ctx:
            s = ctx.session
            plan = Plan(plan_date=date.today(), brand="itest_flagtext")
            s.add(plan)
            await s.flush()
            item = PlanItem(
                plan_id=plan.id, rank=1, assigned_agent="orchestrator", action_type="notify",
                params={"message": "writer_below_index",
                        "flag": {"kind": "writer_below_index", "writer": "Q. Tester",
                                 "relative_index": 0.42, "articles": 7}},
                rationale="Flag surfaced: writer_below_index.",
            )
            s.add(item)
            await s.flush()

            updated = await backfill_flag_descriptions(s)
            assert updated >= 1
            await s.refresh(item)
            assert "Q. Tester" in item.params["message"] and "42%" in item.params["message"]
            assert "Q. Tester" in item.rationale and "cohort average" in item.rationale

            # Idempotent: a second pass leaves this (now-current) item unchanged.
            before = dict(item.params)
            await backfill_flag_descriptions(s)
            await s.refresh(item)
            assert item.params == before

            await s.execute(delete(PlanItem).where(PlanItem.plan_id == plan.id))
            await s.execute(delete(Plan).where(Plan.id == plan.id))
            await s.commit()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no reachable Postgres: {exc}")
