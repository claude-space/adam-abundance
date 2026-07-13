"""Integration-test fixtures.

pytest-asyncio runs each test in its own (function-scoped) event loop, but the
async engine + asyncpg pool are cached module-level in ``switchboard.db.base``.
An asyncpg connection is bound to the loop that created it, so reusing the cached
pool across tests fails. Disposing the cached engines after each test makes the
next test recreate a pool in its own loop. (The real app runs a single loop, so
this only affects the test harness.)
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
async def _dispose_engines_between_tests():
    yield
    from switchboard.db.base import dispose_engines

    await dispose_engines()
