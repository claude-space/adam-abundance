"""Shared fixtures for the whole test suite.

Two concerns handled here:

1. **Engine disposal between tests.** ``switchboard.db.base`` caches the async
   engine + asyncpg pool at module level, but pytest-asyncio gives each test its
   own (function-scoped) event loop and an asyncpg pool is bound to the loop that
   created it. Disposing after every test forces the next test to recreate a pool
   in its own loop. (The real app runs one loop, so this only matters in tests.)

2. **DB-backed tests self-skip when no Postgres is reachable.** The pure-logic
   suite must still run on a machine with no database; DB-backed unit tests take
   the ``ctx``/``db_ctx`` fixture, which skips them when ``DATABASE_URL`` points at
   nothing reachable. CI provides a real Postgres service, so there they run.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
async def _dispose_engines_between_tests():
    yield
    from switchboard.db.base import dispose_engines

    await dispose_engines()


@pytest.fixture
async def db_ctx():
    """A live :class:`~switchboard.context.RunContext` bound to ``DATABASE_URL``.

    Skips the test when no Postgres is reachable so the rest of the suite still
    runs without a database. Assumes the schema is migrated (``alembic upgrade
    head``) — CI does this before the run.
    """
    from switchboard.context import RunContext

    try:
        async with RunContext.open() as c:
            await c.store.expire_stale()  # cheap connectivity ping
            yield c
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no reachable Postgres: {exc}")
