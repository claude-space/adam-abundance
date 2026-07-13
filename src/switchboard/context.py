"""Per-run context: the handful of collaborators an adapter or agent needs,
bundled so they aren't re-instantiated or passed around piecemeal.

A ``RunContext`` is scoped to one DB transaction (``session_scope``). Open one
per morning-cycle stage / per observe pass:

    async with RunContext.open() as ctx:
        await SomeAgent(ctx).observe("hotcars")

Everything an agent legitimately touches — shared memory (``store``), the
governor, config, and the credentials layer — hangs off here. Crucially, there
is **no handle to another agent** on the context: the only cross-agent channel
is ``store`` (PRD §5, §14).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings, get_settings
from .credentials import Credentials
from .db.base import session_scope
from .governor import Governor
from .memory.store import MemoryStore


@dataclass
class RunContext:
    session: AsyncSession
    store: MemoryStore
    governor: Governor
    settings: Settings
    creds: Credentials

    @property
    def dry_run_default(self) -> bool:
        return self.settings.dry_run_default

    @classmethod
    @asynccontextmanager
    async def open(cls, url: str | None = None) -> AsyncIterator["RunContext"]:
        settings = get_settings()
        async with session_scope(url) as session:
            yield cls(
                session=session,
                store=MemoryStore(session),
                governor=Governor(session, settings),
                settings=settings,
                creds=settings.creds,
            )
