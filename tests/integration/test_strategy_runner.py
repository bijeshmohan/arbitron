"""Strategy runner, config snapshots, and cursors against real PostgreSQL."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from arbitron.application.strategy_runner import StrategyRunner
from arbitron.clocks import ManualClock
from arbitron.domain.market import Candle
from arbitron.domain.portfolio import PortfolioView
from arbitron.domain.signals import Signal
from arbitron.domain.strategies import StrategyCursor
from arbitron.market.freshness import DataIssue, DataIssueCode, DataQualityReport
from arbitron.ports.repositories import ConcurrencyConflict
from arbitron.strategy.base import Strategy, StrategyError
from arbitron.strategy.evaluation import replay
from arbitron.strategy.implementations.sma_cross import SmaCross
from tests.factories import FIVE_MIN, SMA_CONFIG, T0
from tests.integration.conftest import UowFactory
from tests.unit.test_strategy import SERIES, CountingStrategy

SYMBOL = "BTC/USDT"


def ok(symbol: str = SYMBOL) -> DataQualityReport:
    return DataQualityReport(symbol, T0, ())


def blocked() -> DataQualityReport:
    return DataQualityReport(SYMBOL, T0, (DataIssue(DataIssueCode.STALE, SYMBOL, "old"),))


async def store(uow: UowFactory, candles: Sequence[Candle]) -> None:
    async with uow() as tx:
        await tx.candles.upsert(candles)
        await tx.commit()


def runner(
    uow: UowFactory,
    clock: ManualClock,
    strategy: Strategy | None = None,
    *,
    max_catch_up: int = 500,
) -> StrategyRunner:
    return StrategyRunner(
        strategy or SmaCross(fast=2, slow=4),
        exchange_id="binance",
        timeframe=FIVE_MIN,
        uow_factory=uow,
        clock=clock,
        max_catch_up=max_catch_up,
    )


async def stored_signal_times(engine: AsyncEngine) -> list[datetime]:
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT source_candle_time FROM signals ORDER BY 1"))
        return [row[0] for row in rows]


async def event_types(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT event_type FROM system_events ORDER BY id"))
        return [row[0] for row in rows]


class TestRunner:
    async def test_live_run_matches_replay_exactly(self, uow: UowFactory) -> None:
        """Acceptance: identical inputs and config produce identical signals, live or replayed."""
        clock = ManualClock(SERIES[-1].close_time)
        strategy_runner = runner(uow, clock)
        live: list[Signal] = []
        # Candles arrive one at a time, as they would in production.
        await store(uow, SERIES[:5])
        live.extend((await strategy_runner.process(SYMBOL, ok())).signals)
        for candle in SERIES[5:]:
            await store(uow, [candle])
            live.extend((await strategy_runner.process(SYMBOL, ok())).signals)

        expected = [
            s
            for s in replay(
                SmaCross(fast=2, slow=4),
                SERIES,
                timeframe=FIVE_MIN,
                portfolio=PortfolioView(as_of=T0),
            )
            if s.source_candle_time >= SERIES[4].open_time
        ]
        assert [replace(s, created_at=T0) for s in live] == [
            replace(s, created_at=T0) for s in expected
        ]
        assert len(live) >= 2

    async def test_first_run_evaluates_only_the_latest_candle(self, uow: UowFactory) -> None:
        await store(uow, SERIES)
        strategy = CountingStrategy(lookback=3)

        result = await runner(uow, ManualClock(T0), strategy).process(SYMBOL, ok())

        assert result.evaluated == (SERIES[-1].open_time,)
        assert len(result.signals) == 1
        assert result.cursor == SERIES[-1].open_time

    async def test_signals_config_and_cursor_are_persisted(
        self, uow: UowFactory, engine: AsyncEngine
    ) -> None:
        await store(uow, SERIES[:10])
        clock = ManualClock(T0 + timedelta(hours=2))
        strategy_runner = runner(uow, clock, CountingStrategy(lookback=3))
        await strategy_runner.process(SYMBOL, ok())

        await store(uow, SERIES[10:13])
        result = await strategy_runner.process(SYMBOL, ok())

        assert result.evaluated == tuple(c.open_time for c in SERIES[10:13])
        assert await stored_signal_times(engine) == [c.open_time for c in SERIES[9:13]]
        async with uow() as tx:
            cursor = await tx.strategy_cursors.get(
                strategy_runner.config_hash, "binance", SYMBOL, FIVE_MIN
            )
            snapshot = await tx.strategy_configs.get(strategy_runner.config_hash)
        assert cursor is not None
        assert cursor.last_open_time == SERIES[12].open_time
        assert snapshot is not None
        assert snapshot.strategy_id == "counting"

    async def test_nothing_new_means_nothing_evaluated(self, uow: UowFactory) -> None:
        await store(uow, SERIES)
        strategy_runner = runner(uow, ManualClock(T0), CountingStrategy(lookback=3))
        await strategy_runner.process(SYMBOL, ok())

        again = await strategy_runner.process(SYMBOL, ok())

        assert again.evaluated == ()
        assert again.signals == ()
        assert again.cursor == SERIES[-1].open_time

    async def test_crash_before_cursor_commit_does_not_duplicate_signals(
        self, uow: UowFactory, engine: AsyncEngine
    ) -> None:
        await store(uow, SERIES[:10])
        clock = ManualClock(T0)
        first = runner(uow, clock, CountingStrategy(lookback=3))
        await first.process(SYMBOL, ok())
        # Simulate a crash after signals were persisted but with the cursor lost:
        # a fresh cursor-less state re-evaluates the same latest candle.
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM strategy_cursors"))

        again = await runner(uow, clock, CountingStrategy(lookback=3)).process(SYMBOL, ok())

        assert again.evaluated == (SERIES[9].open_time,)
        assert again.signals == ()  # already persisted: deduplicated
        assert await stored_signal_times(engine) == [SERIES[9].open_time]

    async def test_blocked_quality_gate_prevents_evaluation(
        self, uow: UowFactory, engine: AsyncEngine
    ) -> None:
        await store(uow, SERIES)
        strategy = CountingStrategy(lookback=3)

        result = await runner(uow, ManualClock(T0), strategy).process(SYMBOL, blocked())

        assert strategy.seen == []
        assert result.evaluated == ()
        assert "stale" in result.skipped[0]
        assert await event_types(engine) == ["strategy.blocked"]
        assert await stored_signal_times(engine) == []

    async def test_quality_report_must_match_symbol(self, uow: UowFactory) -> None:
        with pytest.raises(ValueError, match="quality report is for"):
            await runner(uow, ManualClock(T0)).process(SYMBOL, ok("ETH/USDT"))

    async def test_incomplete_windows_are_skipped_and_recorded(
        self, uow: UowFactory, engine: AsyncEngine
    ) -> None:
        await store(uow, SERIES[:6])
        strategy_runner = runner(uow, ManualClock(T0), CountingStrategy(lookback=3))
        await strategy_runner.process(SYMBOL, ok())
        await store(uow, SERIES[7:10])  # SERIES[6] is missing

        result = await strategy_runner.process(SYMBOL, ok())

        assert result.evaluated == (SERIES[9].open_time,)
        assert len(result.skipped) == 2
        assert await event_types(engine) == ["strategy.window_incomplete"] * 2
        assert result.cursor == SERIES[9].open_time

    async def test_strategy_failure_records_event_and_keeps_cursor(
        self, uow: UowFactory, engine: AsyncEngine
    ) -> None:
        await store(uow, SERIES[:6])
        clock = ManualClock(T0)
        good = runner(uow, clock, CountingStrategy(lookback=3))
        await good.process(SYMBOL, ok())
        await store(uow, SERIES[6:8])

        failing = CountingStrategy(lookback=3, raise_error=True)
        with pytest.raises(StrategyError):
            await runner(uow, clock, failing).process(SYMBOL, ok())

        assert await event_types(engine) == ["strategy.failed"]
        async with uow() as tx:
            cursor = await tx.strategy_cursors.get(good.config_hash, "binance", SYMBOL, FIVE_MIN)
        assert cursor is not None
        assert cursor.last_open_time == SERIES[5].open_time

    async def test_catch_up_is_bounded(self, uow: UowFactory, engine: AsyncEngine) -> None:
        await store(uow, SERIES[:4])
        strategy_runner = runner(uow, ManualClock(T0), CountingStrategy(lookback=3), max_catch_up=5)
        await strategy_runner.process(SYMBOL, ok())
        await store(uow, SERIES[4:])

        result = await strategy_runner.process(SYMBOL, ok())

        assert result.evaluated == tuple(c.open_time for c in SERIES[-5:])
        assert "catch-up limit" in result.skipped[0]
        assert await event_types(engine) == ["strategy.catch_up_truncated"]

    async def test_different_parameters_are_a_separate_stream(self, uow: UowFactory) -> None:
        await store(uow, SERIES)
        a = runner(uow, ManualClock(T0), SmaCross(fast=2, slow=4))
        b = runner(uow, ManualClock(T0), SmaCross(fast=3, slow=5))

        assert a.config_hash != b.config_hash
        await a.process(SYMBOL, ok())
        await b.process(SYMBOL, ok())
        async with uow() as tx:
            assert await tx.strategy_cursors.get(a.config_hash, "binance", SYMBOL, FIVE_MIN)
            assert await tx.strategy_cursors.get(b.config_hash, "binance", SYMBOL, FIVE_MIN)


class TestCursorRepository:
    def cursor(self, minutes: int) -> StrategyCursor:
        return StrategyCursor(
            SMA_CONFIG.config_hash,
            "binance",
            SYMBOL,
            FIVE_MIN,
            T0 + timedelta(minutes=minutes),
            T0 + timedelta(minutes=minutes),
        )

    async def setup_config(self, uow: UowFactory) -> None:
        async with uow() as tx:
            assert await tx.strategy_configs.add_if_absent(SMA_CONFIG, created_at=T0)
            assert not await tx.strategy_configs.add_if_absent(SMA_CONFIG, created_at=T0)
            await tx.commit()

    async def test_create_advance_and_conflict(self, uow: UowFactory) -> None:
        await self.setup_config(uow)
        async with uow() as tx:
            await tx.strategy_cursors.save(None, self.cursor(0))
            await tx.strategy_cursors.save(self.cursor(0), self.cursor(5))
            await tx.commit()
        async with uow() as tx:
            assert await tx.strategy_cursors.get(
                SMA_CONFIG.config_hash, "binance", SYMBOL, FIVE_MIN
            ) == self.cursor(5)
            with pytest.raises(ConcurrencyConflict):
                await tx.strategy_cursors.save(self.cursor(0), self.cursor(10))
            with pytest.raises(ConcurrencyConflict):
                await tx.strategy_cursors.save(None, self.cursor(10))

    async def test_cannot_move_backwards_or_change_identity(self, uow: UowFactory) -> None:
        await self.setup_config(uow)
        async with uow() as tx:
            await tx.strategy_cursors.save(None, self.cursor(5))
            with pytest.raises(ValueError, match="backwards"):
                await tx.strategy_cursors.save(self.cursor(5), self.cursor(5))
            with pytest.raises(ValueError, match="cannot change"):
                await tx.strategy_cursors.save(
                    self.cursor(5), replace(self.cursor(10), symbol="ETH/USDT")
                )

    async def test_cursor_requires_config_snapshot(self, uow: UowFactory) -> None:
        from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

        async with uow() as tx:
            with pytest.raises(IntegrityError, match="fk_strategy_cursors_config_hash"):
                await tx.strategy_cursors.save(None, self.cursor(0))
