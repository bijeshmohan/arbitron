"""Equity history: daily-loss baseline and drawdown high-water mark."""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncEngine

from arbitron.application.snapshot_builder import SnapshotBuilder, start_of_utc_day
from arbitron.clocks import ManualClock
from arbitron.domain.portfolio import EquitySnapshot
from arbitron.market.freshness import DataQualityReport
from arbitron.paper.broker import PaperBroker
from arbitron.persistence.db import create_session_factory
from tests.factories import FIVE_MIN, T0
from tests.integration.conftest import UowFactory
from tests.integration.test_market_ingestion import MARKET, NOW, FakeVenue, history
from tests.integration.test_paper_trading import MODEL

SYMBOL = "BTC/USDT"


async def builder(engine: AsyncEngine, uow: UowFactory, clock: ManualClock) -> SnapshotBuilder:
    venue = FakeVenue(clock, history(200))
    broker = PaperBroker(
        session_factory=create_session_factory(engine),
        account="t",
        exchange_id="binance",
        quote_currency="USDT",
        market_data=venue,
        markets=lambda: {SYMBOL: MARKET},
        fill_model=MODEL,
        clock=clock,
    )
    await broker.ensure_funded(Decimal(1000))
    return SnapshotBuilder(
        exchange_id="binance",
        quote_currency="USDT",
        symbols=[SYMBOL],
        timeframe=FIVE_MIN,
        market_data=venue,
        account=broker,
        markets=lambda: {SYMBOL: MARKET},
        uow_factory=uow,
        clock=clock,
        trading_enabled=True,
    )


OK = {SYMBOL: DataQualityReport(SYMBOL, T0, ())}


async def test_first_snapshot_of_the_day_is_the_baseline(
    uow: UowFactory, engine: AsyncEngine
) -> None:
    clock = ManualClock(NOW)
    async with uow() as tx:
        # Yesterday's higher equity sets the peak but not today's baseline.
        await tx.equity_snapshots.add(
            EquitySnapshot(start_of_utc_day(NOW) - timedelta(hours=1), "USDT", Decimal(1200))
        )
        await tx.equity_snapshots.add(
            EquitySnapshot(start_of_utc_day(NOW) + timedelta(minutes=1), "USDT", Decimal(1050))
        )
        await tx.commit()

    snapshot = await (await builder(engine, uow, clock)).build(OK, system_ready=True)

    assert snapshot.day_start_equity == Decimal(1050)
    assert snapshot.peak_equity == Decimal(1200)
    assert snapshot.notes["equity"] == "1000"
    async with uow() as tx:
        latest = await tx.equity_snapshots.latest("USDT")
    assert latest is not None
    assert latest.equity == Decimal(1000)


async def test_without_history_current_equity_is_baseline_and_peak(
    uow: UowFactory, engine: AsyncEngine
) -> None:
    async with uow() as tx:
        await tx.candles.upsert(history(100))
        await tx.commit()

    snapshot = await (await builder(engine, uow, ManualClock(NOW))).build(OK, system_ready=True)

    assert snapshot.day_start_equity == Decimal(1000)
    assert snapshot.peak_equity == Decimal(1000)
    assert snapshot.reconciliation_clean
    assert snapshot.market_data_ok == {SYMBOL: True}
    assert snapshot.marks[SYMBOL] > 0


async def test_unhealthy_quality_report_is_carried(uow: UowFactory, engine: AsyncEngine) -> None:
    snapshot = await (await builder(engine, uow, ManualClock(NOW))).build({}, system_ready=False)

    assert snapshot.market_data_ok == {SYMBOL: False}
    assert not snapshot.system_ready
