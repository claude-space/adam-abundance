"""DB-backed unit tests for :class:`switchboard.users.UserRepo` (RBAC, PRD §9.1).

Runs against a real Postgres opened via ``RunContext.open()``. ``UserRepo`` is not
on the context, so each test builds ``UserRepo(ctx.session)``. Self-skips when no
DB is reachable.

The ``app_user`` table holds REAL users, so:
  * every test email is a clearly-fake ``utest+<tag>-<uuid>@valnetinc.com`` and is
    deleted by the autouse ``_clean_users`` teardown — real rows are never touched;
  * the "first user -> global_admin" bootstrap can't be reproduced by emptying the
    shared table, so that one branch is exercised by stubbing ``_count`` (the write
    is still real);
  * ``auth.admins`` / ``auth.default_role`` are injected onto a *copy* of settings
    (never the process-wide cache) via ``dataclasses.replace``.

Note on real behavior: ``auth.default_role`` is ``portfolio_admin`` in this env
(NOT "viewer"), so a subsequent non-admin user is provisioned as portfolio_admin.
Tests read the value from settings rather than hard-coding it.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import delete

from switchboard.context import RunContext
from switchboard.db.models import AppUser
from switchboard.rbac import Role, is_valid_role
from switchboard.users import UserRepo

EMAIL_PREFIX = "utest+"


def _email(tag: str) -> str:
    return f"{EMAIL_PREFIX}{tag}-{uuid4().hex[:8]}@valnetinc.com"


@pytest.fixture
async def ctx():
    try:
        async with RunContext.open() as c:
            await c.store.expire_stale()  # cheap connectivity ping
            yield c
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no reachable Postgres: {exc}")


@pytest.fixture(autouse=True)
async def _clean_users():
    """Delete only the fake test users this module provisions (after each test)."""
    yield
    try:
        async with RunContext.open() as c:
            await c.session.execute(delete(AppUser).where(AppUser.email.like(f"{EMAIL_PREFIX}%")))
            await c.session.commit()
    except Exception:  # noqa: BLE001 — no DB / nothing to scrub
        pass


# -- provision ----------------------------------------------------------------


async def test_provision_subsequent_user_gets_default_role(ctx):
    repo = UserRepo(ctx.session)
    await repo.provision(_email("sentinel"))  # guarantee the table is non-empty
    u = await repo.provision(_email("sub"))
    default_role = ctx.settings.auth.default_role
    assert is_valid_role(default_role)
    assert u.role == default_role  # 'portfolio_admin' in this env, not 'viewer'
    assert u.brands is None


async def test_provision_first_user_bootstraps_global_admin(ctx):
    repo = UserRepo(ctx.session)
    # The shared DB already has real users, so count==0 can't happen naturally;
    # stub _count to drive the bootstrap branch. The row is still really written.
    async def _zero() -> int:
        return 0

    repo._count = _zero  # type: ignore[assignment]
    u = await repo.provision(_email("boot"))
    assert u.role == Role.GLOBAL_ADMIN.value


async def test_provision_admin_email_gets_global_admin(ctx):
    email = _email("admin")
    repo = UserRepo(ctx.session)
    # inject the email into auth.admins on a COPY of settings (upper-cased to prove
    # the match is case-insensitive); never mutate the cached global settings.
    repo.settings = replace(ctx.settings, auth=replace(ctx.settings.auth, admins=(email.upper(),)))
    await repo.provision(_email("filler"))  # ensure this isn't the first-user path
    u = await repo.provision(email)
    assert u.role == Role.GLOBAL_ADMIN.value


async def test_provision_invalid_default_role_falls_back_to_viewer(ctx):
    repo = UserRepo(ctx.session)
    repo.settings = replace(ctx.settings, auth=replace(ctx.settings.auth, default_role="not_a_role", admins=()))
    await repo.provision(_email("filler2"))  # ensure the default-role path is used
    u = await repo.provision(_email("baddefault"))
    assert u.role == Role.VIEWER.value


async def test_provision_is_idempotent(ctx):
    repo = UserRepo(ctx.session)
    email = _email("idem")
    await repo.provision(email, name="First")
    await repo.set_role(email, Role.VIEWER.value)
    again = await repo.provision(email, name="Ignored")
    assert again.email == email
    assert again.role == Role.VIEWER.value  # existing role preserved, not reset
    assert again.name == "First"            # name not overwritten


async def test_provision_lowercases_email_and_get_is_case_insensitive(ctx):
    repo = UserRepo(ctx.session)
    mixed = f"{EMAIL_PREFIX}Case-{uuid4().hex[:8]}@Valnetinc.COM"
    u = await repo.provision(mixed)
    assert u.email == mixed.lower()
    assert await repo.get(mixed) is not None
    assert await repo.get(mixed.lower()) is not None
    assert await repo.get(mixed.upper()) is not None


async def test_provision_persists_name_and_created_at(ctx):
    repo = UserRepo(ctx.session)
    email = _email("rt")
    u = await repo.provision(email, name="Round Trip")
    assert u.created_at is not None and u.name == "Round Trip"
    await ctx.session.refresh(u)  # confirm the row reached the DB
    assert u.name == "Round Trip"


# -- get / list ---------------------------------------------------------------


async def test_get_unknown_returns_none(ctx):
    assert await UserRepo(ctx.session).get(_email("ghost")) is None


async def test_list_includes_provisioned_users_ordered_by_created_at(ctx):
    repo = UserRepo(ctx.session)
    e1, e2 = _email("list1"), _email("list2")
    await repo.provision(e1)
    await repo.provision(e2)
    rows = await repo.list()
    emails = {u.email for u in rows}
    assert {e1, e2} <= emails
    assert all(isinstance(u, AppUser) for u in rows)
    created = [u.created_at for u in rows]
    assert created == sorted(created)  # order_by(created_at)


# -- set_role -----------------------------------------------------------------


async def test_set_role_updates_and_persists(ctx):
    repo = UserRepo(ctx.session)
    email = _email("setrole")
    await repo.provision(email)
    u = await repo.set_role(email, Role.PORTFOLIO_ADMIN.value)
    assert u.role == Role.PORTFOLIO_ADMIN.value
    assert u.updated_at is not None
    await ctx.session.refresh(u)  # confirm the change reached the DB
    assert u.role == Role.PORTFOLIO_ADMIN.value


async def test_set_role_invalid_role_raises(ctx):
    repo = UserRepo(ctx.session)
    email = _email("badrole")
    await repo.provision(email)
    with pytest.raises(ValueError):
        await repo.set_role(email, "wizard")


async def test_set_role_unknown_user_raises(ctx):
    with pytest.raises(ValueError):
        # valid role + unknown user -> falls through to the unknown-user check
        await UserRepo(ctx.session).set_role(_email("nouser"), Role.VIEWER.value)


async def test_set_role_brand_user_keeps_brands(ctx):
    repo = UserRepo(ctx.session)
    email = _email("branduser")
    await repo.provision(email)
    u = await repo.set_role(email, Role.BRAND_USER.value, brands=["hotcars", "carbuzz"])
    assert u.role == Role.BRAND_USER.value
    assert u.brands == ["hotcars", "carbuzz"]


async def test_set_role_nonbrand_role_clears_brands(ctx):
    repo = UserRepo(ctx.session)
    email = _email("clearbrands")
    await repo.provision(email)
    await repo.set_role(email, Role.BRAND_USER.value, brands=["hotcars"])
    u = await repo.set_role(email, Role.PORTFOLIO_ADMIN.value, brands=["hotcars"])
    assert u.role == Role.PORTFOLIO_ADMIN.value
    assert u.brands is None  # brands ignored unless role is brand_user
    v = await repo.set_role(email, Role.VIEWER.value, brands=["carbuzz"])
    assert v.brands is None


async def test_set_role_accepts_every_valid_role(ctx):
    repo = UserRepo(ctx.session)
    email = _email("allroles")
    await repo.provision(email)
    for role in Role:
        u = await repo.set_role(email, role.value, brands=["hotcars"])
        assert u.role == role.value
        assert u.brands == (["hotcars"] if role == Role.BRAND_USER else None)
