"""Async SQLAlchemy engine + session plumbing for the shared memory.

Postgres (not SQLite) because agents and adapters read/write concurrently
(PRD §7.1). The engine is created lazily and cached per-URL so importing this
module never opens a connection (keeps ``migrations/env.py`` and tests cheap).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

_ENGINES: dict[str, AsyncEngine] = {}
_SESSIONMAKERS: dict[str, async_sessionmaker[AsyncSession]] = {}


class Base(DeclarativeBase):
    """Declarative base for all shared-memory tables."""


def _resolve_url(url: str | None) -> str:
    if url:
        return url
    # Lazy import to avoid a config <-> db import cycle.
    from ..config import get_settings

    return get_settings().database_url


def get_engine(url: str | None = None) -> AsyncEngine:
    resolved = _resolve_url(url)
    if resolved not in _ENGINES:
        connect_args: dict = {}
        # Supabase/pgBouncer transaction-pooler compatibility: asyncpg's prepared
        # statement cache breaks under transaction pooling. Set
        # DB_STATEMENT_CACHE_SIZE=0 to disable it (prefer the session pooler /
        # direct connection on port 5432, where this isn't needed).
        cache = os.environ.get("DB_STATEMENT_CACHE_SIZE")
        if cache is not None and cache.strip() != "":
            connect_args["statement_cache_size"] = int(cache)
        _ENGINES[resolved] = create_async_engine(
            resolved,
            pool_pre_ping=True,
            future=True,
            connect_args=connect_args,
        )
    return _ENGINES[resolved]


def get_sessionmaker(url: str | None = None) -> async_sessionmaker[AsyncSession]:
    resolved = _resolve_url(url)
    if resolved not in _SESSIONMAKERS:
        _SESSIONMAKERS[resolved] = async_sessionmaker(
            get_engine(resolved), expire_on_commit=False, class_=AsyncSession
        )
    return _SESSIONMAKERS[resolved]


@asynccontextmanager
async def session_scope(url: str | None = None) -> AsyncIterator[AsyncSession]:
    """Transactional scope: commit on success, rollback on error, always close."""
    maker = get_sessionmaker(url)
    session = maker()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def dispose_engines() -> None:
    """Dispose all cached engines (call on shutdown)."""
    for engine in _ENGINES.values():
        await engine.dispose()
    _ENGINES.clear()
    _SESSIONMAKERS.clear()
