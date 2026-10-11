import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from arbitron.persistence.lock import (
    InstanceLock,
    LockLostError,
    LockNotAcquiredError,
    lock_key,
)


def make_lock(engine: AsyncEngine, mode: str = "paper") -> InstanceLock:
    return InstanceLock(engine, exchange_id="binance", account="main", mode=mode)


def test_lock_key_is_stable_and_scoped() -> None:
    assert lock_key("binance", "main", "paper") == lock_key("binance", "main", "paper")
    assert lock_key("binance", "main", "paper") != lock_key("binance", "main", "sandbox")
    assert -(2**63) <= lock_key("binance", "main", "paper") < 2**63


async def test_second_instance_is_refused(engine: AsyncEngine) -> None:
    async with make_lock(engine) as first:
        await first.check()
        with pytest.raises(LockNotAcquiredError):
            await make_lock(engine).acquire()


async def test_different_mode_is_independent(engine: AsyncEngine) -> None:
    async with make_lock(engine, "paper"), make_lock(engine, "sandbox") as sandbox:
        await sandbox.check()


async def test_lock_is_reacquirable_after_release(engine: AsyncEngine) -> None:
    async with make_lock(engine):
        pass
    async with make_lock(engine) as again:
        await again.check()


async def test_check_detects_terminated_connection(engine: AsyncEngine) -> None:
    lock = make_lock(engine)
    await lock.acquire()
    try:
        async with engine.connect() as admin:
            await admin.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_locks "
                    "WHERE locktype = 'advisory' AND objsubid = 1 "
                    "AND ((classid::bigint << 32) | objid::bigint) = :key"
                ),
                {"key": lock.key},
            )
        with pytest.raises(LockLostError):
            await lock.check()
        # Repeated checks (e.g. a periodic supervisor) keep reporting the loss.
        with pytest.raises(LockLostError):
            await lock.check()
        # Another instance can now take over: exactly why loss is critical.
        async with make_lock(engine) as successor:
            await successor.check()
    finally:
        await lock.release()


async def test_check_without_acquire_fails(engine: AsyncEngine) -> None:
    with pytest.raises(LockLostError, match="not held"):
        await make_lock(engine).check()


@pytest.mark.parametrize("account", ["acct-positive", "acct-negative"])
async def test_check_works_for_both_key_signs(engine: AsyncEngine, account: str) -> None:
    # Find accounts whose keys have each sign, so the pg_locks reconstruction is exercised.
    want_negative = account.endswith("negative")
    suffix = 0
    while (lock_key("binance", f"{account}{suffix}", "paper") < 0) is not want_negative:
        suffix += 1
    lock = InstanceLock(engine, exchange_id="binance", account=f"{account}{suffix}", mode="paper")
    async with lock:
        await lock.check()
