import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from arbitron.persistence.db import (
    SchemaNotCurrentError,
    alembic_config,
    create_engine,
    expected_head,
    verify_schema_current,
)


@pytest.fixture
def scratch_url(postgres_url: str) -> Iterator[str]:
    """An empty, separate database so migration tests never disturb other tests."""
    import psycopg  # noqa: PLC0415

    url = make_url(postgres_url)
    admin = url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS migration_scratch_test")
        conn.execute("CREATE DATABASE migration_scratch_test")
    yield url.set(database="migration_scratch_test").render_as_string(hide_password=False)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("DROP DATABASE migration_scratch_test WITH (FORCE)")


@pytest.fixture
async def scratch_engine(scratch_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(scratch_url, pool_size=1)
    yield engine
    await engine.dispose()


async def test_empty_database_is_not_current(scratch_engine: AsyncEngine) -> None:
    with pytest.raises(SchemaNotCurrentError, match="no revision"):
        await verify_schema_current(scratch_engine)


async def test_upgrade_downgrade_upgrade_from_empty(
    scratch_url: str, scratch_engine: AsyncEngine
) -> None:
    config = alembic_config(scratch_url)

    # Alembic's async env runs its own event loop; keep it off the test's loop.
    await asyncio.to_thread(command.upgrade, config, "head")
    assert await verify_schema_current(scratch_engine) == expected_head()

    await asyncio.to_thread(command.downgrade, config, "base")
    async with scratch_engine.connect() as conn:
        tables = await conn.scalar(
            text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
        )
    assert tables == 1  # only alembic_version remains

    await asyncio.to_thread(command.upgrade, config, "head")
    assert await verify_schema_current(scratch_engine) == expected_head()


def test_models_match_migrations(database_url: str) -> None:
    # Raises if autogenerate would emit any operation (model/migration drift).
    command.check(alembic_config(database_url))


def test_engine_rejects_non_psycopg_urls() -> None:
    with pytest.raises(ValueError, match="postgresql\\+psycopg"):
        create_engine("postgresql+asyncpg://u:p@localhost/db")
