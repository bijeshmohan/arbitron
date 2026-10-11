"""Async engine/session lifecycle and migration verification."""

from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

REQUIRED_DRIVER = "postgresql+psycopg"
# Shipped inside the package so an installed build can migrate without the repo.
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


class SchemaNotCurrentError(RuntimeError):
    """The database schema is not at the migration head this code expects."""


def create_engine(url: str, *, pool_size: int = 5) -> AsyncEngine:
    """Create the async engine. Only ``postgresql+psycopg://`` URLs are accepted."""
    parsed = make_url(url)
    if parsed.drivername != REQUIRED_DRIVER:
        msg = f"database URL must use {REQUIRED_DRIVER}://, got {parsed.drivername}://"
        raise ValueError(msg)
    return create_async_engine(
        parsed,
        pool_size=pool_size,
        pool_pre_ping=True,
        # All timestamps are timestamptz; make the session explicit about UTC.
        connect_args={"options": "-c timezone=UTC"},
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


def alembic_config(url: str | None = None) -> Config:
    """Programmatic Alembic config; the URL is passed in, never stored in a file."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if url is not None:
        config.attributes["database_url"] = url
    return config


def expected_head() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    if head is None:  # pragma: no cover - the repository always ships migrations
        msg = "no migration head found"
        raise SchemaNotCurrentError(msg)
    return head


async def verify_schema_current(engine: AsyncEngine) -> str:
    """Return the current revision, or raise ``SchemaNotCurrentError``.

    Startup calls this before anything touches the database (plan §14.1):
    running against an unmigrated or newer schema fails closed.
    """

    def _current(connection: Connection) -> str | None:
        return MigrationContext.configure(connection).get_current_revision()

    async with engine.connect() as connection:
        current = await connection.run_sync(_current)
    head = expected_head()
    if current != head:
        msg = f"database schema is at {current or 'no revision'}, expected {head}"
        raise SchemaNotCurrentError(msg)
    return head
