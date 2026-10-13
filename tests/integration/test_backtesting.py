"""Backfill, backtest service, sweeps, and experiment records against real PostgreSQL."""

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from arbitron.application.backtesting import BacktestDataError, BacktestService
from arbitron.clocks import ManualClock
from arbitron.config.settings import ArbitronConfig
from arbitron.domain.market import Candle
from arbitron.market.backfill import Backfiller
from tests.factories import FIVE_MIN, T0, make_candle
from tests.integration.conftest import UowFactory
from tests.integration.test_market_ingestion import MARKET, FakeVenue
from tests.unit.test_config import example_data

SYMBOL = "BTC/USDT"


def wave(count: int) -> list[Candle]:
    """A triangle wave (0.02 per candle, period 40) so SMA crossovers happen.

    Steps stay well inside the IOC slippage cap, so next-open fills succeed.
    """
    prices = [Decimal(100) + Decimal("0.02") * min(i % 40, 40 - i % 40) for i in range(count)]
    return [
        make_candle(
            open_time=T0 + i * FIVE_MIN.duration,
            ingested_at=T0 + (i + 1) * FIVE_MIN.duration,
            open=p,
            close=p,
            high=p,
            low=p,
        )
        for i, p in enumerate(prices)
    ]


def config(**strategy: object) -> ArbitronConfig:
    data = example_data()
    data["strategy"]["parameters"] = {"fast": 3, "slow": 8, **strategy}
    return ArbitronConfig.model_validate(data)


async def seed(uow: UowFactory, candles: list[Candle]) -> None:
    async with uow() as tx:
        await tx.markets.save(MARKET, observed_at=T0)
        await tx.candles.upsert(candles)
        await tx.commit()


def at(index: int) -> datetime:
    return T0 + index * FIVE_MIN.duration


class TestBackfill:
    async def test_pages_store_markets_and_report_gaps(self, uow: UowFactory) -> None:
        candles = wave(250)
        del candles[100:103]
        venue = FakeVenue(ManualClock(at(260)), candles)
        backfiller = Backfiller(
            market_data=venue,
            uow_factory=uow,
            clock=venue.clock,
            exchange_id="binance",
            timeframe=FIVE_MIN,
            page_limit=60,
        )

        await backfiller.store_markets((SYMBOL,))
        result = await backfiller.run(SYMBOL, at(0), at(250))

        assert result.fetched == 247
        assert result.written == 247
        assert result.pages == 5
        assert result.gaps == [(at(100), at(102))]
        async with uow() as tx:
            assert await tx.markets.get("binance", SYMBOL) == MARKET
            stored = await tx.candles.closed_range(
                "binance", SYMBOL, FIVE_MIN, start=at(0), end=at(250)
            )
        assert len(stored) == 247

    async def test_rerun_writes_nothing_new(self, uow: UowFactory) -> None:
        venue = FakeVenue(ManualClock(at(120)), wave(100))
        backfiller = Backfiller(
            market_data=venue,
            uow_factory=uow,
            clock=venue.clock,
            exchange_id="binance",
            timeframe=FIVE_MIN,
        )
        await backfiller.run(SYMBOL, at(0), at(100))

        again = await backfiller.run(SYMBOL, at(0), at(100))

        assert again.fetched == 100
        assert again.written == 0  # stored closed candles are never rewritten


class TestBacktestService:
    async def test_run_is_recorded_and_reproducible(self, uow: UowFactory) -> None:
        await seed(uow, wave(400))
        service = BacktestService(
            config(), uow_factory=uow, clock=ManualClock(at(500)), version="test"
        )

        first = await service.run(start=at(20), end=at(400))
        second = await service.run(start=at(20), end=at(400))

        assert first.record.metrics == second.record.metrics
        assert first.record.data_fingerprint == second.record.data_fingerprint
        assert first.record.id != second.record.id
        assert first.result.trades, "the wave should trigger trades"
        async with uow() as tx:
            stored = await tx.experiments.get(first.record.id)
        assert stored == first.record
        assert stored is not None
        assert stored.assumptions["fill_timing"] == "next candle open"
        assert stored.parameters == {"fast": 3, "slow": 8}

    async def test_warm_up_comes_from_before_start(self, uow: UowFactory) -> None:
        await seed(uow, wave(400))
        service = BacktestService(
            config(), uow_factory=uow, clock=ManualClock(at(500)), version="test"
        )

        run = await service.run(start=at(100), end=at(200))

        assert run.result.skipped_windows == 0  # the 9-candle lookback was loaded
        assert run.result.equity[0].time == at(101)
        assert run.record.candle_count == 109  # 100 in range + 9 warm-up

    async def test_parameter_overrides_change_identity(self, uow: UowFactory) -> None:
        await seed(uow, wave(400))
        service = BacktestService(
            config(), uow_factory=uow, clock=ManualClock(at(500)), version="test"
        )

        default = await service.run(start=at(20), end=at(400))
        other = await service.run(start=at(20), end=at(400), parameters={"fast": 5})

        assert other.record.parameters["fast"] == 5
        assert other.record.config_hash != default.record.config_hash

    async def test_missing_market_rules_are_explained(self, uow: UowFactory) -> None:
        async with uow() as tx:
            await tx.candles.upsert(wave(50))
            await tx.commit()
        service = BacktestService(
            config(), uow_factory=uow, clock=ManualClock(at(60)), version="test"
        )

        with pytest.raises(BacktestDataError, match="market backfill"):
            await service.run(start=at(10), end=at(50))

    async def test_missing_candles_are_explained(self, uow: UowFactory) -> None:
        await seed(uow, wave(50))
        service = BacktestService(
            config(), uow_factory=uow, clock=ManualClock(at(500)), version="test"
        )

        with pytest.raises(BacktestDataError, match="no stored candles"):
            await service.run(start=at(100), end=at(200))

    async def test_sweep_reports_each_combination_and_invalid_ones(self, uow: UowFactory) -> None:
        await seed(uow, wave(400))
        service = BacktestService(
            config(), uow_factory=uow, clock=ManualClock(at(500)), version="test"
        )

        rows = await service.sweep(
            start=at(20), split=at(200), end=at(400), grid={"fast": [3, 9], "slow": [8]}
        )

        assert [r.parameters for r in rows] == ["fast=3, slow=8", "fast=9, slow=8"]
        valid, invalid = rows
        assert valid.in_sample is not None
        assert valid.out_of_sample is not None
        assert valid.in_sample.end == valid.out_of_sample.start == at(200)
        assert valid.in_sample.label.endswith(":in_sample")
        assert invalid.in_sample is None
        assert "must be smaller" in str(invalid.error)

    async def test_sweep_requires_ordered_dates(self, uow: UowFactory) -> None:
        service = BacktestService(config(), uow_factory=uow, clock=ManualClock(T0), version="test")

        with pytest.raises(ValueError, match="start < split < end"):
            await service.sweep(start=at(10), split=at(5), end=at(20), grid={})

    async def test_experiments_are_append_only(self, uow: UowFactory, engine: AsyncEngine) -> None:
        await seed(uow, wave(200))
        service = BacktestService(
            config(), uow_factory=uow, clock=ManualClock(at(300)), version="test"
        )
        await service.run(start=at(20), end=at(200))

        async with engine.begin() as conn:
            with pytest.raises(DBAPIError, match="append-only"):
                await conn.execute(text("UPDATE backtest_runs SET metrics = '{}'"))


def test_cli_backtest_run_list_show(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    import asyncio  # noqa: PLC0415

    from arbitron.cli import EXIT_OK, main  # noqa: PLC0415
    from arbitron.persistence.db import create_engine, create_session_factory  # noqa: PLC0415
    from arbitron.persistence.unit_of_work import SqlUnitOfWork  # noqa: PLC0415
    from tests.unit.test_cli import EXAMPLE_PAPER  # noqa: PLC0415

    async def seed_db() -> None:
        engine = create_engine(database_url)
        factory = create_session_factory(engine)
        await seed(lambda: SqlUnitOfWork(factory), wave(200))
        await engine.dispose()

    asyncio.run(seed_db())
    monkeypatch.setenv("ARBITRON_DATABASE_URL", database_url)
    out = tmp_path / "reports"

    start, end = at(40).isoformat(), at(200).isoformat()
    assert (
        main(
            [
                "backtest",
                "run",
                str(EXAMPLE_PAPER),
                "--start",
                start,
                "--end",
                end,
                "--param",
                "fast=3",
                "--param",
                "slow=8",
                "--out",
                str(out),
            ]
        )
        == EXIT_OK
    )
    report = capsys.readouterr().out
    assert "## Assumptions" in report
    assert "Past performance in a backtest is not evidence of future returns" in report
    assert len(list(out.glob("backtest-*.json"))) == 1
    assert len(list(out.glob("backtest-*.md"))) == 1

    assert main(["backtest", "list", str(EXAMPLE_PAPER)]) == EXIT_OK
    listing = capsys.readouterr().out
    run_id = listing.split()[0]
    assert "sma_cross" in listing

    assert main(["backtest", "show", str(EXAMPLE_PAPER), run_id]) == EXIT_OK
    assert run_id in capsys.readouterr().out
