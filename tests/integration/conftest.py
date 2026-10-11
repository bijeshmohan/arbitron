"""Integration fixtures: a disposable PostgreSQL migrated to head.

By default a throwaway ``postgres:18-alpine`` container is started via
testcontainers (Docker required). To reuse an existing server, set
``ARBITRON_TEST_DATABASE_URL``; its database name must end in ``_test``
because every test truncates all tables.
"""

import os
from collections.abc import AsyncIterator, Callable, Iterator

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer  # pyright: ignore[reportMissingTypeStubs]

from arbitron.persistence.db import alembic_config, create_engine, create_session_factory
from arbitron.persistence.tables import Base
from arbitron.persistence.unit_of_work import SqlUnitOfWork

POSTGRES_IMAGE = "postgres:18-alpine"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/integration" in str(item.path):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """Server URL whose database tests may freely truncate."""
    configured = os.environ.get("ARBITRON_TEST_DATABASE_URL")
    if configured:
        database = make_url(configured).database or ""
        if not database.endswith("_test"):
            pytest.fail(
                f"ARBITRON_TEST_DATABASE_URL database {database!r} must end in '_test' "
                "(integration tests truncate every table)"
            )
        yield configured
        return
    with PostgresContainer(POSTGRES_IMAGE, dbname="arbitron_test", driver="psycopg") as postgres:
        yield str(postgres.get_connection_url())  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]


@pytest.fixture(scope="session")
def database_url(postgres_url: str) -> str:
    command.upgrade(alembic_config(postgres_url), "head")
    return postgres_url


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(database_url)
    try:
        yield engine
    finally:
        tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        await engine.dispose()


type UowFactory = Callable[[], SqlUnitOfWork]


@pytest.fixture
def uow(engine: AsyncEngine) -> UowFactory:
    factory = create_session_factory(engine)
    return lambda: SqlUnitOfWork(factory)
