"""Single-instance lock (plan §7): a session-level PostgreSQL advisory lock.

The lock lives as long as one dedicated connection. If that connection drops,
PostgreSQL releases the lock and another instance may acquire it, so callers
must treat ``LockLostError`` from ``check()`` as a critical safety failure.
"""

import hashlib
from types import TracebackType
from typing import Self

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


class LockNotAcquiredError(RuntimeError):
    """Another instance already holds the lock for this account and mode."""


class LockLostError(RuntimeError):
    """The lock connection failed or the lock is no longer held."""


def lock_key(exchange_id: str, account: str, mode: str) -> int:
    """Stable signed 64-bit advisory-lock key for ``(exchange, account, mode)``."""
    digest = hashlib.sha256(f"arbitron:{exchange_id}:{account}:{mode}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class InstanceLock:
    """Async context manager holding the advisory lock on a dedicated connection."""

    def __init__(self, engine: AsyncEngine, *, exchange_id: str, account: str, mode: str) -> None:
        self._engine = engine
        self.key = lock_key(exchange_id, account, mode)
        self._connection: AsyncConnection | None = None

    async def acquire(self) -> None:
        # AUTOCOMMIT so the connection never sits idle inside a transaction for
        # the life of the process.
        connection = await self._engine.connect()
        try:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": self.key}
            )
        except BaseException:
            await connection.close()
            raise
        if not acquired:
            await connection.close()
            msg = f"instance lock {self.key} is held by another process"
            raise LockNotAcquiredError(msg)
        self._connection = connection

    async def check(self) -> None:
        """Verify the lock is still held; raise ``LockLostError`` otherwise."""
        if self._connection is None:
            msg = "instance lock is not held"
            raise LockLostError(msg)
        try:
            held = await self._connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                    "AND pid = pg_backend_pid() AND granted AND objsubid = 1 "
                    "AND ((classid::bigint << 32) | objid::bigint) = :key)"
                ),
                {"key": self.key},
            )
        except SQLAlchemyError as exc:
            msg = "instance lock connection failed"
            raise LockLostError(msg) from exc
        if not held:
            msg = f"instance lock {self.key} is no longer held"
            raise LockLostError(msg)

    async def release(self) -> None:
        connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": self.key})
        except SQLAlchemyError:
            # The connection is already dead, and PostgreSQL released the lock
            # with the session. Nothing more to undo.
            pass
        finally:
            await connection.close()

    async def __aenter__(self) -> Self:
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.release()
