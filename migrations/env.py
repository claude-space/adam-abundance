"""Alembic environment (async). The DB URL is pulled from the credentials/config
layer at runtime, so no connection string with a password lives in a committed
file (PRD §8: secrets never in code/logs)."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

# Import the metadata + all models so autogenerate/DDL sees every table.
from switchboard.config import get_settings
from switchboard.db.base import Base, get_engine
from switchboard.db import models  # noqa: F401  (registers tables on Base.metadata)
from switchboard.logging_ import setup_logging

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
setup_logging()

target_metadata = Base.metadata


def _url() -> str:
    return get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = get_engine(_url())
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
