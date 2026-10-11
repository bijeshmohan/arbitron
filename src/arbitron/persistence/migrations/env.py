"""Alembic environment (async, psycopg 3).

The database URL comes from ``config.attributes["database_url"]`` (set by
``arbitron.persistence.db.alembic_config``) or the ``ARBITRON_DATABASE_URL``
environment variable. It is never read from ``alembic.ini``.
"""

import asyncio
import os

from alembic import context
from sqlalchemy import Connection

from arbitron.persistence.db import create_engine
from arbitron.persistence.tables import Base

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    url = config.attributes.get("database_url") or os.environ.get("ARBITRON_DATABASE_URL")
    if not url:
        msg = "set ARBITRON_DATABASE_URL (postgresql+psycopg://...) to run migrations"
        raise RuntimeError(msg)
    return str(url)


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    engine = create_engine(_database_url(), pool_size=1)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_configure)
            await connection.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(url=_database_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_online())
