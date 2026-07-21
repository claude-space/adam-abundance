"""HTTP-level tests for the JSON API's WRITE + parameterized-DETAIL endpoints.

Companion to ``test_api_json.py`` (which covers the read endpoints, the auth
gate, and the logo proxy). This file exercises the mutation routes and the
``/{id}`` detail routes that the read-only suite does not: it CREATES the row an
endpoint needs (a trend, pipeline, artifact/job, plan+item, persona, style
profile, user) via the real repos, hits the endpoint, asserts the ACTUAL effect
(status change / row written / response shape), and then DELETES everything it
made. Every created row carries a ``utest_api`` marker (headline / cluster_key /
requested_by / created_by / name / email) or the fake brand ``utest_api_brand``
so the autouse cleanup can find and remove it — nothing is left in the shared DB.

Auth pattern is copied from ``test_api_json.py``: httpx ``AsyncClient`` +
``ASGITransport`` so every request runs on the test's own event loop, plus the
``/auth/dev-login`` fixture. The whole module self-skips when no Postgres is
reachable.

Notes on the two "expensive/destructive" routes: ``/api/cycle`` runs the morning
cycle *synchronously*, and ``/api/trends/scan`` kicks a real scan in a background
task — so those are asserted at their guard/refusal paths (401 / 400 / 403), and
the endpoints whose success queues a background job sweep are tested with that
sweep neutralised (``no_bg``) so no generation/LLM/network work is triggered and
no *foreign* queued jobs get processed.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

DEV_EMAIL = "andrew.marks@valnetinc.com"        # provisioned global_admin (verified)
MARK = "utest_api"                              # substring stamped on every created row
FAKE_BRAND = "utest_api_brand"                  # non-real brand → isolates persona/style rows
REAL_BRAND = "hotcars"                          # a configured brand_key


def _uniq(prefix: str = "") -> str:
    return f"{prefix}{MARK}_{uuid.uuid4().hex[:8]}"


async def _require_db():
    from switchboard.context import RunContext

    try:
        async with RunContext.open() as c:
            await c.store.expire_stale()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no reachable Postgres: {exc}")


# --- clients ---------------------------------------------------------------

@pytest.fixture
async def anon():
    """An unauthenticated ASGI client."""
    from switchboard.api.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False
    ) as ac:
        yield ac


@pytest.fixture
async def api():
    """A dev-logged-in ASGI client — the dev user is a global_admin (skips if no DB)."""
    await _require_db()
    from switchboard.api.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as ac:
        r = await ac.post("/auth/dev-login", data={"email": DEV_EMAIL})
        assert r.status_code in (200, 302), r.text[:300]
        yield ac


@pytest.fixture
async def viewer():
    """A logged-in client whose role is ``viewer`` — used to prove the RBAC gate
    refuses (403). We pre-create a throwaway ``utest_api`` viewer, then dev-login
    as them (dev-login provisions/returns the existing row, so the session role
    is viewer). The user is removed by the autouse cleanup."""
    await _require_db()
    email = f"{MARK}_viewer@valnetinc.com"
    from switchboard.context import RunContext
    from switchboard.users import UserRepo

    async with RunContext.open() as ctx:
        repo = UserRepo(ctx.session)
        await repo.provision(email, "utest_api viewer")
        await repo.set_role(email, "viewer", None)
    from switchboard.api.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as ac:
        r = await ac.post("/auth/dev-login", data={"email": email})
        assert r.status_code in (200, 302), r.text[:300]
        who = await ac.get("/api/me")
        assert who.json().get("role") == "viewer", who.text[:200]
        yield ac


@pytest.fixture
def no_bg(monkeypatch):
    """Neutralise the post-response background work so success paths that queue a
    job sweep / trend scan do no generation, no network, and never touch foreign
    queued jobs. The handlers ``from ..trends.pipeline import run_job_sweep`` (and
    ``..trends.scout import run_trend_scan``) at call time, so patching the module
    attribute is picked up."""
    import switchboard.trends.pipeline as _pipe
    import switchboard.trends.scout as _scout

    async def _noop_sweep(*a, **k):
        return {"ok": 0, "pending": 0, "failed": 0}

    async def _noop_scan(*a, **k):
        return None

    monkeypatch.setattr(_pipe, "run_job_sweep", _noop_sweep, raising=True)
    monkeypatch.setattr(_scout, "run_trend_scan", _noop_scan, raising=True)


# --- autouse cleanup: delete every utest_api-marked row after each test ----

@pytest.fixture(autouse=True)
async def _cleanup_utest_rows():
    """Snapshot the trend_score_weight high-water id (that table has no natural
    marker column), then after the test delete every row we could have created.
    Best-effort + FK-ordered; silent when no DB (the anon-only 401 tests)."""
    from sqlalchemy import text

    wmax = None
    try:
        from switchboard.context import RunContext

        async with RunContext.open() as ctx:
            wmax = (await ctx.session.execute(
                text("SELECT COALESCE(MAX(id), 0) FROM trend_score_weight"))).scalar_one()
    except Exception:  # noqa: BLE001 — DB unreachable → nothing was created either
        wmax = None

    yield

    if wmax is None:
        return
    like = f"%{MARK}%"
    try:
        from switchboard.context import RunContext
        from switchboard.db.base import dispose_engines

        async with RunContext.open() as ctx:
            s = ctx.session
            # content_job cascades from content_pipeline (FK ondelete=CASCADE), so
            # deleting the pipelines is enough. trend→pipeline is SET NULL, so
            # pipelines MUST go before trends (while trend_id still matches).
            await s.execute(text(
                "DELETE FROM content_pipeline WHERE requested_by LIKE :m OR brand = :b "
                "OR trend_id IN (SELECT id FROM trend "
                "                WHERE headline LIKE :m OR cluster_key LIKE :m OR brand = :b)"),
                {"m": like, "b": FAKE_BRAND})
            await s.execute(text(
                "DELETE FROM trend WHERE headline LIKE :m OR cluster_key LIKE :m OR brand = :b"),
                {"m": like, "b": FAKE_BRAND})
            # plan_item cascades from plan (FK ondelete=CASCADE).
            await s.execute(text("DELETE FROM plan WHERE created_by LIKE :m OR brand = :b"),
                            {"m": like, "b": FAKE_BRAND})
            await s.execute(text(
                "DELETE FROM writer_persona WHERE created_by LIKE :m OR name LIKE :m OR brand = :b"),
                {"m": like, "b": FAKE_BRAND})
            await s.execute(text("DELETE FROM writer_style_profile WHERE brand = :b"),
                            {"b": FAKE_BRAND})
            await s.execute(text("DELETE FROM app_user WHERE email LIKE :m"), {"m": like})
            await s.execute(text("DELETE FROM trend_score_weight WHERE id > :wmax"),
                            {"wmax": wmax})
        # Dispose here too: guarantees no engine created by this cleanup leaks onto
        # the next test's event loop, regardless of fixture-finalisation order.
        await dispose_engines()
    except Exception:  # noqa: BLE001 — cleanup is best-effort
        pass


# --- row-creation helpers (each stamps the utest_api marker) ----------------

async def _mk_trend(*, status: str = "detected", brand: str = REAL_BRAND,
                    headline: str | None = None) -> int:
    from switchboard.context import RunContext
    from switchboard.db.models import Trend

    now = datetime.now(timezone.utc)
    async with RunContext.open() as ctx:
        t = Trend(brand=brand, cluster_key=_uniq("cluster:"),
                  headline=headline or _uniq("headline "), score=42.0,
                  status=status, origin="scout", last_seen_at=now,
                  expires_at=now + timedelta(hours=48))
        ctx.session.add(t)
        await ctx.session.flush()
        return t.id


async def _mk_pipeline(*, brand: str = REAL_BRAND, trend_id: int | None = None,
                       status: str = "pending_approval",
                       content_types: list[str] | None = None) -> int:
    from switchboard.context import RunContext
    from switchboard.db.models import ContentPipeline

    async with RunContext.open() as ctx:
        p = ContentPipeline(trend_id=trend_id, brand=brand, status=status,
                            requested_by=_uniq("requested_by:"),
                            content_types=content_types or ["article"])
        ctx.session.add(p)
        await ctx.session.flush()
        return p.id


async def _mk_job(*, pipeline_id: int, status: str = "preview_ready",
                  content_type: str = "article", published: bool = False) -> int:
    from switchboard.context import RunContext
    from switchboard.db.models import ContentJob

    now = datetime.now(timezone.utc)
    async with RunContext.open() as ctx:
        j = ContentJob(
            pipeline_id=pipeline_id, content_type=content_type, transport="llm",
            status=("published" if published else status),
            preview_meta={"title": _uniq("Title "), "word_count": 321},
            reviewed_by=DEV_EMAIL if published else None,
            reviewed_at=now if published else None)
        ctx.session.add(j)
        await ctx.session.flush()
        return j.id


async def _mk_plan_with_item(*, brand: str = REAL_BRAND) -> tuple[int, int]:
    from switchboard.context import RunContext
    from switchboard.orchestrator.plans import PlanRepo

    async with RunContext.open() as ctx:
        repo = PlanRepo(ctx.session)
        plan = await repo.create_plan(brand, date.today(), created_by=_uniq("createdby:"))
        item = await repo.add_item(plan, rank=1, assigned_agent="trend_scout",
                                   action_type="trigger_ideation",
                                   params={"title": _uniq("Item ")},
                                   rationale="utest_api rationale",
                                   cost_estimate={"llm_micros": 1000})
        return plan.id, item.id


async def _mk_persona(*, brand: str = FAKE_BRAND, enabled: bool = True) -> int:
    from switchboard.context import RunContext
    from switchboard.trends import personas as P

    async with RunContext.open() as ctx:
        p = await P.create_house_persona(ctx.session, brand, _uniq("Persona "),
                                         style_brief="utest_api brief",
                                         created_by=_uniq("cb:"))
        if not enabled:
            await P.set_enabled(ctx.session, p.id, False)
        return p.id


async def _mk_style_profile(*, brand: str = FAKE_BRAND, active: bool = False) -> int:
    from switchboard.context import RunContext
    from switchboard.db.models import WriterStyleProfile

    async with RunContext.open() as ctx:
        sp = WriterStyleProfile(brand=brand, version=uuid.uuid4().int % 1_000_000 + 900_000,
                                source_authors=[MARK], features={}, active=active)
        ctx.session.add(sp)
        await ctx.session.flush()
        return sp.id


async def _mk_user(*, role: str = "portfolio_admin") -> str:
    email = f"{MARK}_target_{uuid.uuid4().hex[:6]}@valnetinc.com"
    from switchboard.context import RunContext
    from switchboard.users import UserRepo

    async with RunContext.open() as ctx:
        repo = UserRepo(ctx.session)
        await repo.provision(email, "utest_api target")
        if role != "portfolio_admin":
            await repo.set_role(email, role, None)
    return email


# --- read-back helpers (return plain snapshots; session is closed on return) -

async def _pipeline_snap(pid: int):
    from switchboard.context import RunContext
    from switchboard.trends.repo import PipelineRepo

    async with RunContext.open() as ctx:
        p = await PipelineRepo(ctx.session).get(pid)
        if p is None:
            return None
        return {"status": p.status, "jobs": len(p.jobs or []), "brand": p.brand,
                "approved_by": p.approved_by, "declined_by": p.declined_by,
                "close_reason": p.close_reason}


async def _trend_status(tid: int):
    from switchboard.context import RunContext
    from switchboard.trends.repo import TrendRepo

    async with RunContext.open() as ctx:
        t = await TrendRepo(ctx.session).get(tid)
        return None if t is None else t.status


async def _pipelines_for_trend(tid: int):
    from switchboard.context import RunContext
    from switchboard.trends.repo import TrendRepo

    async with RunContext.open() as ctx:
        t = await TrendRepo(ctx.session).get(tid)
        return [] if t is None else [{"id": p.id, "status": p.status} for p in t.pipelines]


async def _persona_enabled(pid: int):
    from switchboard.context import RunContext
    from switchboard.trends import personas as P

    async with RunContext.open() as ctx:
        p = await P.get_persona(ctx.session, pid)
        return None if p is None else p.enabled


async def _profile_active(spid: int):
    from switchboard.context import RunContext
    from switchboard.db.models import WriterStyleProfile

    async with RunContext.open() as ctx:
        sp = await ctx.session.get(WriterStyleProfile, spid)
        return None if sp is None else sp.active


async def _user_role(email: str):
    from switchboard.context import RunContext
    from switchboard.users import UserRepo

    async with RunContext.open() as ctx:
        u = await UserRepo(ctx.session).get(email)
        return None if u is None else (u.role, list(u.brands or []))


async def _item_snap(item_id: int):
    from switchboard.context import RunContext
    from switchboard.db.models import PlanItem

    async with RunContext.open() as ctx:
        it = await ctx.session.get(PlanItem, item_id)
        return None if it is None else (it.status, it.dry_run)


# ============================================================================
# Detail GETs — real created row (200 + shape) AND a bogus id (404)
# ============================================================================

async def test_trend_detail_ok(api):
    tid = await _mk_trend()
    r = await api.get(f"/api/trends/{tid}")
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["trend"]["id"] == tid
    assert MARK in body["trend"]["headline"]
    assert "article" in body["content_types"]
    assert "default_types" in body and body["kill_switch"] is False
    assert body["trend"]["may_approve"] is True     # dev user is global_admin


async def test_trend_detail_bogus_404(api):
    assert (await api.get("/api/trends/999000001")).status_code == 404


async def test_pipeline_detail_ok(api):
    pid = await _mk_pipeline(content_types=["article"])
    r = await api.get(f"/api/pipelines/{pid}")
    assert r.status_code == 200, r.text[:300]
    p = r.json()["pipeline"]
    assert p["id"] == pid and p["brand"] == REAL_BRAND
    assert p["status"] == "pending_approval"
    assert isinstance(p["steps"], list)
    assert p["may_approve"] is True


async def test_pipeline_detail_bogus_404(api):
    assert (await api.get("/api/pipelines/999000001")).status_code == 404


async def test_agent_detail_ok(api):
    overview = (await api.get("/api/agents")).json()["agents"]
    if not overview:
        pytest.skip("no agents in fleet overview")
    key = overview[0]["key"]
    r = await api.get(f"/api/agents/{key}")
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["agent"]["key"] == key
    assert "cost" in body and "spark" in body["cost"]
    assert isinstance(body["events"], list) and isinstance(body["pipelines"], list)


async def test_agent_detail_bogus_404(api):
    assert (await api.get("/api/agents/utest_api_nope")).status_code == 404


async def test_plan_detail_ok(api):
    plan_id, _item = await _mk_plan_with_item()
    r = await api.get(f"/api/plans/{plan_id}")
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["id"] == plan_id and body["brand"] == REAL_BRAND
    assert isinstance(body["items"], list) and len(body["items"]) >= 1
    assert "slack_brief" in body and body["can_approve"] is True


async def test_plan_detail_bogus_404(api):
    assert (await api.get("/api/plans/999000001")).status_code == 404


async def test_artifact_detail_ok(api):
    pid = await _mk_pipeline(trend_id=None)
    jid = await _mk_job(pipeline_id=pid, status="preview_ready")
    r = await api.get(f"/api/artifacts/AR-{jid:04d}")
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["id"] == f"AR-{jid:04d}"
    assert isinstance(body["article"], dict)
    assert isinstance(body["breakdown"], list) and isinstance(body["timeline"], list)
    assert "signals" in body and body["may_approve"] is True


async def test_artifact_detail_bogus_404(api):
    assert (await api.get("/api/artifacts/AR-9990001")).status_code == 404


async def test_artifact_detail_malformed_id_404(api):
    # _parse_artifact_id can't parse an int → 404 (never a 500).
    assert (await api.get("/api/artifacts/not-an-id")).status_code == 404


async def test_ledger_detail_ok(api):
    pid = await _mk_pipeline(trend_id=None)
    jid = await _mk_job(pipeline_id=pid, published=True)
    r = await api.get(f"/api/ledger/AR-{jid:04d}")
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["id"] == f"AR-{jid:04d}"
    assert body["brand"] == REAL_BRAND
    assert isinstance(body["channels"], list) and "benchmark" in body


async def test_ledger_detail_bogus_404(api):
    assert (await api.get("/api/ledger/AR-9990001")).status_code == 404


async def test_ledger_detail_unpublished_is_404(api):
    # /api/ledger only surfaces *published* jobs; a preview_ready one is not found.
    pid = await _mk_pipeline(trend_id=None)
    jid = await _mk_job(pipeline_id=pid, status="preview_ready")
    assert (await api.get(f"/api/ledger/AR-{jid:04d}")).status_code == 404


# ============================================================================
# Writes — assert the effect + validation (400/404). (401 covered en masse below.)
# ============================================================================

# --- /api/trends/{id}/trigger ----------------------------------------------

async def test_trend_trigger_creates_pipeline(api, no_bg):
    tid = await _mk_trend(status="detected")
    r = await api.post(f"/api/trends/{tid}/trigger", json={})
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["ok"] is True and body["approved"] is False
    assert isinstance(body["pipeline_id"], int)
    # Effect: a pending pipeline now exists for the trend, and the trend advanced
    # detected → proposed.
    pipes = await _pipelines_for_trend(tid)
    assert any(p["id"] == body["pipeline_id"] and p["status"] == "pending_approval"
               for p in pipes)
    assert await _trend_status(tid) == "proposed"


async def test_trend_trigger_bogus_404(api, no_bg):
    assert (await api.post("/api/trends/999000001/trigger", json={})).status_code == 404


async def test_trend_trigger_wrong_status_400(api, no_bg):
    # A completed trend can no longer be triggered.
    tid = await _mk_trend(status="completed")
    r = await api.post(f"/api/trends/{tid}/trigger", json={})
    assert r.status_code == 400, r.text[:300]
    assert "can no longer be triggered" in r.json()["detail"]


# --- /api/pipelines/{id}/approve -------------------------------------------

async def test_pipeline_approve_effect(api, no_bg):
    pid = await _mk_pipeline(trend_id=None, content_types=["article"])
    r = await api.post(f"/api/pipelines/{pid}/approve", json={})
    assert r.status_code == 200, r.text[:300]
    assert r.json() == {"ok": True, "pipeline_id": pid, "status": "approved"}
    snap = await _pipeline_snap(pid)
    # approve_and_start moves pending_approval → (approved) → generating and queues
    # one job per content type. The neutralised sweep leaves it at generating.
    assert snap["status"] == "generating"
    assert snap["jobs"] == 1
    assert snap["approved_by"] == DEV_EMAIL


async def test_pipeline_approve_bogus_404(api, no_bg):
    assert (await api.post("/api/pipelines/999000001/approve", json={})).status_code == 404


async def test_pipeline_approve_bad_state_400(api, no_bg):
    # A declined pipeline cannot be approved (invalid lifecycle transition).
    pid = await _mk_pipeline(trend_id=None, status="declined")
    r = await api.post(f"/api/pipelines/{pid}/approve", json={})
    assert r.status_code == 400, r.text[:300]


# --- /api/pipelines/{id}/decline -------------------------------------------

async def test_pipeline_decline_effect(api):
    pid = await _mk_pipeline(trend_id=None)
    r = await api.post(f"/api/pipelines/{pid}/decline", json={"reason": "utest_api no"})
    assert r.status_code == 200, r.text[:300]
    assert r.json() == {"ok": True, "pipeline_id": pid, "status": "declined"}
    snap = await _pipeline_snap(pid)
    assert snap["status"] == "declined"
    assert snap["declined_by"] == DEV_EMAIL and snap["close_reason"] == "utest_api no"


async def test_pipeline_decline_bogus_404(api):
    assert (await api.post("/api/pipelines/999000001/decline", json={})).status_code == 404


async def test_pipeline_decline_twice_400(api):
    pid = await _mk_pipeline(trend_id=None)
    assert (await api.post(f"/api/pipelines/{pid}/decline", json={})).status_code == 200
    # Second decline: declined is terminal → invalid transition → 400.
    assert (await api.post(f"/api/pipelines/{pid}/decline", json={})).status_code == 400


# --- /api/personas/house + /{id}/enable ------------------------------------

async def test_personas_house_create_and_conflict(api):
    name = _uniq("House ")
    body = {"brand": REAL_BRAND, "name": name, "style_brief": "punchy, factual, utest_api"}
    r = await api.post("/api/personas/house", json=body)
    assert r.status_code == 200, r.text[:300]
    out = r.json()
    assert out["ok"] is True and out["name"] == name and isinstance(out["id"], int)
    # Same brand+kind+name again → unique-constraint → 409.
    r2 = await api.post("/api/personas/house", json=body)
    assert r2.status_code == 409, r2.text[:300]


async def test_personas_house_missing_fields_400(api):
    r = await api.post("/api/personas/house", json={"brand": REAL_BRAND, "name": _uniq("H ")})
    assert r.status_code == 400, r.text[:300]


async def test_personas_house_unknown_brand_400(api):
    r = await api.post("/api/personas/house",
                       json={"brand": FAKE_BRAND, "name": _uniq("H "), "style_brief": "x"})
    assert r.status_code == 400
    assert "unknown brand" in r.json()["detail"]


async def test_personas_enable_toggle(api):
    pid = await _mk_persona(enabled=True)
    r = await api.post(f"/api/personas/{pid}/enable", json={"enabled": False})
    assert r.status_code == 200, r.text[:300]
    assert r.json() == {"ok": True, "id": pid, "enabled": False}
    assert await _persona_enabled(pid) is False
    r2 = await api.post(f"/api/personas/{pid}/enable", json={"enabled": True})
    assert r2.json()["enabled"] is True
    assert await _persona_enabled(pid) is True


async def test_personas_enable_bogus_404(api):
    assert (await api.post("/api/personas/999000001/enable", json={"enabled": True})).status_code == 404


# --- /api/trend-score-weights (POST + /reset) ------------------------------

async def test_trend_score_weights_set_and_reset(api):
    # Set one known weight away from its default, confirm it takes, then reset.
    r = await api.post("/api/trend-score-weights", json={"weights": {"watchlist": 22.5}})
    assert r.status_code == 200, r.text[:300]
    assert "watchlist" in r.json()["updated"]

    got = (await api.get("/api/trend-score-weights")).json()
    wl = next(w for w in got["weights"] if w["key"] == "watchlist")
    assert wl["value"] == 22.5 and wl["customized"] is True

    rr = await api.post("/api/trend-score-weights/reset", json={})
    assert rr.status_code == 200, rr.text[:300]
    assert rr.json()["ok"] is True and isinstance(rr.json()["reset"], int)
    wl2 = next(w for w in (await api.get("/api/trend-score-weights")).json()["weights"]
               if w["key"] == "watchlist")
    assert wl2["customized"] is False           # back to the shipped default


async def test_trend_score_weights_bad_body_400(api):
    r = await api.post("/api/trend-score-weights", json={"weights": "not-a-dict"})
    assert r.status_code == 400, r.text[:300]


# --- /api/writers/activate --------------------------------------------------

async def test_writers_activate_effect(api):
    # Isolated on the fake brand so activation never flips a real brand's profile.
    sp = await _mk_style_profile(brand=FAKE_BRAND, active=False)
    r = await api.post("/api/writers/activate", json={"profile_id": sp})
    assert r.status_code == 200, r.text[:300]
    out = r.json()
    assert out["ok"] is True and out["brand"] == FAKE_BRAND
    assert await _profile_active(sp) is True


async def test_writers_activate_bogus_404(api):
    r = await api.post("/api/writers/activate", json={"profile_id": 999000001})
    assert r.status_code == 404, r.text[:300]


async def test_writers_activate_missing_id_400(api):
    assert (await api.post("/api/writers/activate", json={})).status_code == 400


# --- /api/users/set-role ----------------------------------------------------

async def test_users_set_role_effect(api):
    email = await _mk_user()
    r = await api.post("/api/users/set-role",
                       json={"email": email, "role": "brand_user", "brands": [REAL_BRAND]})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["ok"] is True
    assert await _user_role(email) == ("brand_user", [REAL_BRAND])


async def test_users_set_role_invalid_role_400(api):
    email = await _mk_user()
    r = await api.post("/api/users/set-role", json={"email": email, "role": "utest_api_bogus"})
    assert r.status_code == 400, r.text[:300]


async def test_users_set_role_unknown_user_400(api):
    r = await api.post("/api/users/set-role",
                       json={"email": f"{MARK}_ghost@valnetinc.com", "role": "viewer"})
    assert r.status_code == 400, r.text[:300]


# --- /api/items/{id}/approve + /reject (and the /api/approvals/{id} alias) --

async def test_item_approve_effect(api):
    _plan, item = await _mk_plan_with_item()
    r = await api.post(f"/api/items/{item}/approve", json={})
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["status"] == "approved" and body["dry_run"] is True
    assert await _item_snap(item) == ("approved", True)


async def test_item_approve_go_live_effect(api):
    _plan, item = await _mk_plan_with_item()
    r = await api.post(f"/api/items/{item}/approve", json={"go_live": True})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["dry_run"] is False
    assert await _item_snap(item) == ("approved", False)


async def test_item_reject_effect(api):
    _plan, item = await _mk_plan_with_item()
    r = await api.post(f"/api/items/{item}/reject", json={})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["status"] == "rejected"
    assert (await _item_snap(item))[0] == "rejected"


async def test_item_reject_via_approvals_alias(api):
    # /api/approvals/{id}/reject is registered on the same handler as /api/items/…
    _plan, item = await _mk_plan_with_item()
    r = await api.post(f"/api/approvals/{item}/reject", json={})
    assert r.status_code == 200, r.text[:300]
    assert (await _item_snap(item))[0] == "rejected"


async def test_item_approve_bogus_404(api):
    assert (await api.post("/api/items/999000001/approve", json={})).status_code == 404


# ============================================================================
# Expensive/destructive routes — guard/refusal path only (no heavy work)
# ============================================================================

async def test_cycle_unknown_brand_400(api):
    # The guard rejects an unknown brand BEFORE run_morning_cycle is ever called.
    r = await api.post("/api/cycle", json={"brand": "utest_api_bogus"})
    assert r.status_code == 400, r.text[:300]
    assert "unknown brand" in r.json()["detail"]


async def test_trends_scan_unknown_brand_400(api):
    r = await api.post("/api/trends/scan", json={"brand": "utest_api_bogus"})
    assert r.status_code == 400, r.text[:300]


async def test_trends_scan_ok_guarded(api, no_bg):
    # Valid scope + approver → 200; the actual scan task is neutralised.
    r = await api.post("/api/trends/scan", json={"brand": REAL_BRAND})
    assert r.status_code == 200, r.text[:300]
    assert r.json() == {"ok": True, "scanning": REAL_BRAND}


# ============================================================================
# RBAC gate — a viewer is refused (403) on the approver/admin-only writes
# ============================================================================

async def test_pipeline_approve_forbidden_for_viewer(viewer, no_bg):
    pid = await _mk_pipeline(trend_id=None)
    assert (await viewer.post(f"/api/pipelines/{pid}/approve", json={})).status_code == 403


async def test_pipeline_decline_forbidden_for_viewer(viewer):
    pid = await _mk_pipeline(trend_id=None)
    assert (await viewer.post(f"/api/pipelines/{pid}/decline", json={})).status_code == 403


async def test_trends_scan_forbidden_for_viewer(viewer):
    assert (await viewer.post("/api/trends/scan", json={"brand": REAL_BRAND})).status_code == 403


async def test_item_approve_forbidden_for_viewer(viewer):
    _plan, item = await _mk_plan_with_item()
    assert (await viewer.post(f"/api/items/{item}/approve", json={})).status_code == 403


async def test_personas_house_forbidden_for_viewer(viewer):
    r = await viewer.post("/api/personas/house",
                          json={"brand": REAL_BRAND, "name": _uniq("H "), "style_brief": "x"})
    assert r.status_code == 403


async def test_weights_post_forbidden_for_viewer(viewer):
    assert (await viewer.post("/api/trend-score-weights",
                              json={"weights": {"watchlist": 1.0}})).status_code == 403


async def test_writers_activate_forbidden_for_viewer(viewer):
    sp = await _mk_style_profile()
    assert (await viewer.post("/api/writers/activate", json={"profile_id": sp})).status_code == 403


async def test_users_set_role_forbidden_for_viewer(viewer):
    # set-role requires global_admin specifically (can_manage_users).
    assert (await viewer.post("/api/users/set-role",
                              json={"email": DEV_EMAIL, "role": "viewer"})).status_code == 403


# ============================================================================
# Auth gate (401) — every write + detail route rejects the unauthenticated
# ============================================================================

_WRITE_ROUTES = [
    ("post", "/api/trends/1/trigger"),
    ("post", "/api/pipelines/1/approve"),
    ("post", "/api/pipelines/1/decline"),
    ("post", "/api/personas/house"),
    ("post", "/api/personas/1/enable"),
    ("post", "/api/trend-score-weights"),
    ("post", "/api/trend-score-weights/reset"),
    ("post", "/api/writers/activate"),
    ("post", "/api/users/set-role"),
    ("post", "/api/items/1/approve"),
    ("post", "/api/items/1/reject"),
    ("post", "/api/cycle"),
    ("post", "/api/trends/scan"),
]

_DETAIL_ROUTES = [
    "/api/trends/1",
    "/api/pipelines/1",
    "/api/agents/orchestrator",
    "/api/plans/1",
    "/api/artifacts/AR-0001",
    "/api/ledger/AR-0001",
]


@pytest.mark.parametrize("method,path", _WRITE_ROUTES)
async def test_write_route_requires_auth(anon, method, path):
    r = await anon.request(method, path, json={})
    assert r.status_code == 401, f"{method} {path} -> {r.status_code}"


@pytest.mark.parametrize("path", _DETAIL_ROUTES)
async def test_detail_route_requires_auth(anon, path):
    assert (await anon.get(path)).status_code == 401, path
