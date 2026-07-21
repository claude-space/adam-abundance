"""RunContext (PRD §5, §14): the per-run collaborator bundle.

Only the parts reachable WITHOUT a live database are covered here. ``RunContext``
is a plain dataclass, so the ``dry_run_default`` property and the field set are
tested by direct construction. ``RunContext.open()`` is tested with the DB layer
mocked — ``switchboard.context.session_scope`` is monkeypatched to yield a fake
session, so no engine/connection is created. MemoryStore/Governor only touch the
DB on their async methods (not in ``__init__``), so constructing them here is safe.

The transactional commit/rollback semantics live in ``switchboard.db.base`` and
require a real Postgres; those are left to the DB-backed suite (see the ``db_ctx``
fixture in conftest.py).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import fields
from types import SimpleNamespace

from switchboard.context import RunContext
from switchboard.governor.governor import Governor
from switchboard.memory.store import MemoryStore


def _ctx(dry_run_default=True):
    """A RunContext built from cheap stand-ins (no DB, no real collaborators)."""
    return RunContext(
        session=object(),
        store=object(),
        governor=object(),
        settings=SimpleNamespace(dry_run_default=dry_run_default),
        creds=object(),
    )


# -- dry_run_default property -------------------------------------------------

def test_dry_run_default_true():
    assert _ctx(dry_run_default=True).dry_run_default is True


def test_dry_run_default_false():
    assert _ctx(dry_run_default=False).dry_run_default is False


# -- dataclass shape: no cross-agent handle (PRD §5, §14) ---------------------

def test_context_has_no_agent_handle():
    names = {f.name for f in fields(RunContext)}
    assert names == {"session", "store", "governor", "settings", "creds"}
    assert "agent" not in names  # the only cross-agent channel is `store`


# -- open(): collaborator wiring with the DB mocked ---------------------------

async def test_open_wires_collaborators(monkeypatch):
    fake_session = object()
    captured: dict = {}

    @asynccontextmanager
    async def fake_scope(url=None):
        captured["url"] = url
        yield fake_session

    monkeypatch.setattr("switchboard.context.session_scope", fake_scope)

    async with RunContext.open() as ctx:
        assert ctx.session is fake_session
        # store + governor are real objects, both bound to the same session.
        assert isinstance(ctx.store, MemoryStore)
        assert ctx.store.session is fake_session
        assert isinstance(ctx.governor, Governor)
        assert ctx.governor.session is fake_session
        # settings + creds are threaded through consistently.
        assert ctx.governor.settings is ctx.settings
        assert ctx.creds is ctx.settings.creds
        assert ctx.dry_run_default == ctx.settings.dry_run_default

    assert captured["url"] is None  # default url passed straight to session_scope


async def test_open_passes_url_through(monkeypatch):
    captured: dict = {}

    @asynccontextmanager
    async def fake_scope(url=None):
        captured["url"] = url
        yield object()

    monkeypatch.setattr("switchboard.context.session_scope", fake_scope)

    async with RunContext.open("postgresql+asyncpg://u:p@h/db") as ctx:
        assert ctx.settings is not None
    assert captured["url"] == "postgresql+asyncpg://u:p@h/db"


if __name__ == "__main__":
    import inspect

    for name, fn in sorted(globals().items()):
        if (name.startswith("test_") and callable(fn)
                and not inspect.iscoroutinefunction(fn)
                and not inspect.signature(fn).parameters):
            fn()
            print(f"PASS {name}")
