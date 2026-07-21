"""DB-backed unit tests for :class:`switchboard.memory.store.MemoryStore`.

Runs against a real Postgres (opened via ``RunContext.open()``; the store hangs
off ``ctx.store``). The whole module self-skips when no DB is reachable, mirroring
``tests/integration/test_pipeline.py``.

Isolation: every row this module writes is marked ``source_system`` /
``tool_call_log.agent`` beginning with ``utest_store`` and deleted by the autouse
``_clean_store_rows`` teardown, so the shared DB is never polluted. Queries are
scoped by that marker (+ per-test ``payload.kind``) so real rows can't interfere.

Two Postgres facts shape the tests:
  * ``now()`` is the *transaction* start time — constant across rows written in one
    session — so ordering/``since`` tests set ``created_at`` explicitly.
  * :meth:`MemoryStore.supersede_duplicates` is an un-scoped, table-wide UPDATE;
    that one test rolls its transaction back so it can never touch real rows.

Goes deeper than test_pipeline.py's three smoke tests (roundtrip / fact-gate /
ttl) rather than repeating them.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete

from switchboard.context import RunContext
from switchboard.db.enums import EntryStatus, EntryType
from switchboard.db.models import MemoryEntry, ToolCallLog
from switchboard.interfaces import CostSpec, EntryDraft
from switchboard.logging_ import REDACTED

MARK = "utest_store"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _draft(**kw) -> EntryDraft:
    base = dict(
        type=EntryType.METRIC,
        brand="hotcars",
        source_agent=MARK,
        source_system=MARK,
        payload={"kind": "t"},
    )
    base.update(kw)
    return EntryDraft(**base)


@pytest.fixture
async def ctx():
    try:
        async with RunContext.open() as c:
            await c.store.expire_stale()  # cheap connectivity ping
            yield c
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no reachable Postgres: {exc}")


@pytest.fixture(autouse=True)
async def _clean_store_rows():
    """Delete every marker row this module writes (runs after each test)."""
    yield
    try:
        async with RunContext.open() as c:
            s = c.session
            await s.execute(delete(MemoryEntry).where(MemoryEntry.source_system.like(f"{MARK}%")))
            await s.execute(delete(ToolCallLog).where(ToolCallLog.agent.like(f"{MARK}%")))
            await s.commit()
    except Exception:  # noqa: BLE001 — no DB / nothing to scrub
        pass


# -- writes -------------------------------------------------------------------


async def test_write_persists_all_fields(ctx):
    store = ctx.store
    e = await store.write(_draft(payload={"kind": "rt", "v": 1}))
    assert e.id is not None
    assert e.brand == "hotcars"
    assert e.source_agent == MARK and e.source_system == MARK
    assert e.payload == {"kind": "rt", "v": 1}
    assert e.status == EntryStatus.ACTIVE.value
    assert e.verified is False
    rows = await store.query(source_system=MARK, payload_contains={"kind": "rt"})
    assert any(r.id == e.id for r in rows)


async def test_write_every_entry_type_persists(ctx):
    store = ctx.store
    for t in EntryType:
        e = await store.write(_draft(type=t, payload={"kind": "types", "t": t.value}))
        assert e.type == t
        rows = await store.query(source_system=MARK, types=[t], payload_contains={"t": t.value})
        assert any(r.id == e.id for r in rows), f"{t} not re-queryable"


async def test_write_invalid_brand_raises(ctx):
    with pytest.raises(ValueError):
        await ctx.store.write(_draft(brand="not_a_brand"))


async def test_write_confidence_and_source_urls_persist(ctx):
    e = await ctx.store.write(
        _draft(payload={"kind": "meta"}, confidence=0.75, source_urls=["https://a.com", "https://b.com"])
    )
    await ctx.session.refresh(e)
    assert e.confidence == 0.75
    assert e.source_urls == ["https://a.com", "https://b.com"]


async def test_write_status_default_and_override(ctx):
    store = ctx.store
    active = await store.write(_draft(payload={"kind": "st"}))
    superseded = await store.write(_draft(payload={"kind": "st"}, status=EntryStatus.SUPERSEDED.value))
    assert active.status == "active" and superseded.status == "superseded"
    active_ids = {r.id for r in await store.query(source_system=MARK, payload_contains={"kind": "st"})}
    assert active.id in active_ids and superseded.id not in active_ids


async def test_write_many_applies_gate_to_all(ctx):
    store = ctx.store
    with_gate = await store.write_many(
        [_draft(type=EntryType.FACT, verified=True, payload={"kind": "wm", "i": i}) for i in range(3)],
        fact_gate_ok=True,
    )
    assert len(with_gate) == 3
    assert all(r.type == EntryType.FACT and r.verified is True for r in with_gate)

    without_gate = await store.write_many(
        [_draft(type=EntryType.FACT, verified=True, payload={"kind": "wm2", "i": i}) for i in range(2)]
    )
    assert all(r.type == EntryType.CLAIM and r.verified is False for r in without_gate)


# -- fact gate (PRD §8) -------------------------------------------------------


async def test_fact_gate_downgrades_unverified_fact_to_claim(ctx):
    e = await ctx.store.write(_draft(type=EntryType.FACT, verified=True, payload={"kind": "fg"}))
    assert e.type == EntryType.CLAIM and e.verified is False


async def test_fact_gate_ok_keeps_verified_fact(ctx):
    e = await ctx.store.write(
        _draft(type=EntryType.FACT, verified=True, payload={"kind": "fg"}), fact_gate_ok=True
    )
    assert e.type == EntryType.FACT and e.verified is True


async def test_verified_nonfact_type_kept_but_flag_forced_false(ctx):
    # A non-FACT type is never re-typed, but verified is still forced False w/o gate.
    e = await ctx.store.write(_draft(type=EntryType.METRIC, verified=True, payload={"kind": "fg"}))
    assert e.type == EntryType.METRIC and e.verified is False


async def test_verified_nonfact_kept_with_gate(ctx):
    e = await ctx.store.write(
        _draft(type=EntryType.METRIC, verified=True, payload={"kind": "fg"}), fact_gate_ok=True
    )
    assert e.type == EntryType.METRIC and e.verified is True


async def test_verified_claim_not_retyped(ctx):
    # CLAIM is already unverified-typed; downgrade only clears the flag.
    e = await ctx.store.write(_draft(type=EntryType.CLAIM, verified=True, payload={"kind": "fg"}))
    assert e.type == EntryType.CLAIM and e.verified is False


async def test_unverified_draft_untouched_by_gate(ctx):
    # verified defaults False -> the downgrade branch never runs; type is kept.
    e = await ctx.store.write(_draft(type=EntryType.FACT, verified=False, payload={"kind": "fg"}))
    assert e.type == EntryType.FACT and e.verified is False


# -- query filters ------------------------------------------------------------


async def test_query_brand_includes_portfolio_and_scoping(ctx):
    store = ctx.store
    h = await store.write(_draft(brand="hotcars", payload={"kind": "br"}))
    cb = await store.write(_draft(brand="carbuzz", payload={"kind": "br"}))
    p = await store.write(_draft(brand="portfolio", payload={"kind": "br"}))

    hp = {r.id for r in await store.query(brand="hotcars", source_system=MARK, payload_contains={"kind": "br"})}
    assert h.id in hp and p.id in hp and cb.id not in hp  # brand also matches portfolio

    honly = {
        r.id
        for r in await store.query(
            brand="hotcars", include_portfolio=False, source_system=MARK, payload_contains={"kind": "br"}
        )
    }
    assert honly == {h.id}

    ponly = {r.id for r in await store.query(brand="portfolio", source_system=MARK, payload_contains={"kind": "br"})}
    assert ponly == {p.id}  # brand='portfolio' does not double-expand


async def test_query_types_filter(ctx):
    store = ctx.store
    m = await store.write(_draft(type=EntryType.METRIC, payload={"kind": "tf"}))
    f = await store.write(_draft(type=EntryType.FLAG, payload={"kind": "tf"}))
    only_flag = {r.id for r in await store.query(source_system=MARK, types=[EntryType.FLAG], payload_contains={"kind": "tf"})}
    assert only_flag == {f.id}
    both = {
        r.id
        for r in await store.query(
            source_system=MARK, types=[EntryType.METRIC, EntryType.FLAG], payload_contains={"kind": "tf"}
        )
    }
    assert both == {m.id, f.id}


async def test_query_source_agent_filter(ctx):
    store = ctx.store
    a = await store.write(_draft(source_agent="utest_agent_a", payload={"kind": "sa"}))
    await store.write(_draft(source_agent="utest_agent_b", payload={"kind": "sa"}))
    rows = await store.query(source_system=MARK, source_agent="utest_agent_a", payload_contains={"kind": "sa"})
    assert {r.id for r in rows} == {a.id}


async def test_query_source_system_filter(ctx):
    store = ctx.store
    a = await store.write(_draft(source_system=MARK, payload={"kind": "ss"}))
    b = await store.write(_draft(source_system=f"{MARK}_b", payload={"kind": "ss"}))
    only_b = {r.id for r in await store.query(source_system=f"{MARK}_b", payload_contains={"kind": "ss"})}
    assert only_b == {b.id}
    a_rows = {r.id for r in await store.query(source_system=MARK, payload_contains={"kind": "ss"})}
    assert a.id in a_rows and b.id not in a_rows


async def test_query_verified_filter(ctx):
    store = ctx.store
    v = await store.write(_draft(type=EntryType.METRIC, verified=True, payload={"kind": "vf"}), fact_gate_ok=True)
    u = await store.write(_draft(type=EntryType.METRIC, verified=False, payload={"kind": "vf"}))
    assert {r.id for r in await store.query(source_system=MARK, payload_contains={"kind": "vf"}, verified=True)} == {v.id}
    assert {r.id for r in await store.query(source_system=MARK, payload_contains={"kind": "vf"}, verified=False)} == {u.id}


async def test_query_status_filter_and_default(ctx):
    store = ctx.store
    active = await store.write(_draft(payload={"kind": "qs"}))
    superseded = await store.write(_draft(payload={"kind": "qs"}, status=EntryStatus.SUPERSEDED.value))
    # default status='active'
    default_ids = {r.id for r in await store.query(source_system=MARK, payload_contains={"kind": "qs"})}
    assert default_ids == {active.id}
    # status=None -> no status filter
    all_ids = {r.id for r in await store.query(source_system=MARK, payload_contains={"kind": "qs"}, status=None)}
    assert all_ids == {active.id, superseded.id}
    # explicit status
    sup_ids = {
        r.id
        for r in await store.query(
            source_system=MARK, payload_contains={"kind": "qs"}, status=EntryStatus.SUPERSEDED.value
        )
    }
    assert sup_ids == {superseded.id}


async def test_query_since_filter(ctx):
    store = ctx.store
    e = await store.write(_draft(payload={"kind": "since"}))
    t = datetime(2022, 6, 1, tzinfo=timezone.utc)
    e.created_at = t
    await ctx.session.flush()
    incl = {r.id for r in await store.query(source_system=MARK, payload_contains={"kind": "since"}, since=t - timedelta(seconds=1))}
    assert e.id in incl
    excl = {r.id for r in await store.query(source_system=MARK, payload_contains={"kind": "since"}, since=t + timedelta(seconds=1))}
    assert e.id not in excl


async def test_query_fresh_within_seconds(ctx):
    store = ctx.store
    e = await store.write(_draft(payload={"kind": "fresh"}))
    e.created_at = _now() - timedelta(seconds=100)
    await ctx.session.flush()
    incl = {r.id for r in await store.query(source_system=MARK, payload_contains={"kind": "fresh"}, fresh_within_seconds=1000)}
    assert e.id in incl
    excl = {r.id for r in await store.query(source_system=MARK, payload_contains={"kind": "fresh"}, fresh_within_seconds=10)}
    assert e.id not in excl


async def test_query_payload_contains(ctx):
    store = ctx.store
    e = await store.write(_draft(payload={"kind": "pc", "topic": "ev"}))
    assert e.id in {r.id for r in await store.query(source_system=MARK, payload_contains={"topic": "ev"})}
    assert e.id not in {r.id for r in await store.query(source_system=MARK, payload_contains={"topic": "nope"})}


async def test_query_limit(ctx):
    store = ctx.store
    for i in range(3):
        await store.write(_draft(payload={"kind": "lim", "i": i}))
    rows = await store.query(source_system=MARK, payload_contains={"kind": "lim"}, limit=2)
    assert len(rows) == 2


async def test_query_orders_by_created_at_desc(ctx):
    store = ctx.store
    now = _now()
    e0 = await store.write(_draft(payload={"kind": "ord"}))
    e1 = await store.write(_draft(payload={"kind": "ord"}))
    e2 = await store.write(_draft(payload={"kind": "ord"}))
    e0.created_at = now - timedelta(seconds=3)
    e1.created_at = now - timedelta(seconds=2)
    e2.created_at = now - timedelta(seconds=1)
    await ctx.session.flush()
    rows = await store.query(source_system=MARK, payload_contains={"kind": "ord"})
    assert [r.id for r in rows] == [e2.id, e1.id, e0.id]


async def test_latest_returns_newest_and_none_when_absent(ctx):
    store = ctx.store
    now = _now()
    old = await store.write(_draft(type=EntryType.METRIC, payload={"kind": "lt"}))
    new = await store.write(_draft(type=EntryType.METRIC, payload={"kind": "lt"}))
    old.created_at = now - timedelta(seconds=10)
    new.created_at = now - timedelta(seconds=1)
    await ctx.session.flush()
    got = await store.latest(brand="hotcars", type=EntryType.METRIC, source_system=MARK)
    assert got is not None and got.id == new.id
    assert await store.latest(brand="hotcars", type=EntryType.DECISION, source_system=MARK) is None


# -- TTL / expiry (PRD §7.1) --------------------------------------------------


async def test_resolve_expiry_precedence(ctx):
    store = ctx.store
    explicit = datetime(2030, 1, 1, tzinfo=timezone.utc)
    e = await store.write(_draft(payload={"kind": "exp"}, expires_at=explicit, ttl_seconds=100))
    assert e.expires_at == explicit  # explicit beats ttl_seconds

    e_ttl = await store.write(_draft(payload={"kind": "exp"}, ttl_seconds=3600))
    assert e_ttl.expires_at is not None
    assert 3500 < (e_ttl.expires_at - _now()).total_seconds() < 3700

    e_metric = await store.write(_draft(type=EntryType.METRIC, payload={"kind": "exp"}))
    metric_default = 3 * 24 * 3600
    assert metric_default - 120 < (e_metric.expires_at - _now()).total_seconds() < metric_default + 120

    e_fact = await store.write(_draft(type=EntryType.FACT, payload={"kind": "exp"}))
    assert e_fact.expires_at is None  # facts don't auto-expire


async def test_expire_stale_sweeps_only_past_active(ctx):
    store = ctx.store
    past = await store.write(_draft(payload={"kind": "sw"}, expires_at=_now() - timedelta(seconds=5)))
    future = await store.write(_draft(payload={"kind": "sw"}, expires_at=_now() + timedelta(hours=1)))
    no_expiry = await store.write(_draft(type=EntryType.FACT, payload={"kind": "sw"}))
    swept = await store.expire_stale()
    for e in (past, future, no_expiry):
        await ctx.session.refresh(e)
    assert past.status == EntryStatus.EXPIRED.value
    assert future.status == EntryStatus.ACTIVE.value
    assert no_expiry.status == EntryStatus.ACTIVE.value
    assert swept >= 1


async def test_expire_stale_ignores_nonactive(ctx):
    store = ctx.store
    e = await store.write(
        _draft(payload={"kind": "sw2"}, expires_at=_now() - timedelta(seconds=5), status=EntryStatus.SUPERSEDED.value)
    )
    await store.expire_stale()
    await ctx.session.refresh(e)
    assert e.status == EntryStatus.SUPERSEDED.value  # only 'active' rows are swept


# -- status transitions -------------------------------------------------------


async def test_supersede_marks_rows_and_returns_count(ctx):
    store = ctx.store
    a = await store.write(_draft(payload={"kind": "sup"}))
    b = await store.write(_draft(payload={"kind": "sup"}))
    n = await store.supersede([a.id, b.id])
    assert n == 2
    for e in (a, b):
        await ctx.session.refresh(e)
    assert a.status == EntryStatus.SUPERSEDED.value and b.status == EntryStatus.SUPERSEDED.value
    # both now excluded from the default (active) query
    assert not await store.query(source_system=MARK, payload_contains={"kind": "sup"})


async def test_supersede_empty_returns_zero(ctx):
    assert await ctx.store.supersede([]) == 0


async def test_supersede_nonexistent_returns_zero(ctx):
    assert await ctx.store.supersede([-1, -2]) == 0


async def test_supersede_duplicates_keeps_latest_snapshot(ctx):
    """supersede_duplicates() is a table-wide UPDATE, so this transaction is
    rolled back at the end — it must never mutate real rows."""
    store = ctx.store
    session = ctx.session
    now = _now()
    d0 = await store.write(_draft(type=EntryType.METRIC, payload={"kind": "dupX"}))
    d1 = await store.write(_draft(type=EntryType.METRIC, payload={"kind": "dupX"}))
    d2 = await store.write(_draft(type=EntryType.METRIC, payload={"kind": "dupX"}))
    other = await store.write(_draft(type=EntryType.METRIC, payload={"kind": "otherK"}))
    flag = await store.write(_draft(type=EntryType.FLAG, payload={"kind": "dupX"}))
    d0.created_at = now - timedelta(seconds=3)
    d1.created_at = now - timedelta(seconds=2)
    d2.created_at = now - timedelta(seconds=1)
    await session.flush()
    try:
        swept = await store.supersede_duplicates()
        for e in (d0, d1, d2, other, flag):
            await session.refresh(e)  # re-read within the txn (sees the raw-SQL UPDATE)
        assert d2.status == EntryStatus.ACTIVE.value       # newest snapshot kept
        assert d1.status == EntryStatus.SUPERSEDED.value
        assert d0.status == EntryStatus.SUPERSEDED.value
        assert other.status == EntryStatus.ACTIVE.value    # distinct kind -> own group
        assert flag.status == EntryStatus.ACTIVE.value     # non-snapshot type untouched
        assert swept >= 2
    finally:
        await session.rollback()


# -- audit / tool_call_log ----------------------------------------------------


async def test_log_tool_call_costspec_serialized(ctx):
    row = await ctx.store.log_tool_call(
        agent=MARK, tool="ahrefs", action="read", dry_run=True, brand="hotcars",
        request={"q": "x"}, ok=True, cost=CostSpec(llm_micros=5, usd=0.01),
    )
    assert row.cost == {"ahrefs_units": 0, "llm_micros": 5, "bq_bytes": 0, "usd": 0.01}
    assert row.ok is True and row.dry_run is True and row.agent == MARK and row.action == "read"


async def test_log_tool_call_dict_cost_passthrough(ctx):
    row = await ctx.store.log_tool_call(agent=MARK, tool="t", action="act", dry_run=False, cost={"usd": 1.5})
    assert row.cost == {"usd": 1.5} and row.dry_run is False


async def test_log_tool_call_none_cost_and_request(ctx):
    row = await ctx.store.log_tool_call(agent=MARK, tool="t", action="read", dry_run=True)
    assert row.cost is None and row.request is None


async def test_log_tool_call_redacts_request(ctx):
    secret = "sk-ant-" + "a" * 28  # matches the sk-ant- shape backstop
    row = await ctx.store.log_tool_call(
        agent=MARK, tool="t", action="read", dry_run=True, request={"api_key": secret, "q": "hi"}
    )
    assert row.request["api_key"] == REDACTED
    assert row.request["q"] == "hi"
    assert secret not in json.dumps(row.request)
