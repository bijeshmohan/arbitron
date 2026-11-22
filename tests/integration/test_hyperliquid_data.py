"""Hyperliquid candle history against real PostgreSQL: venue separation and a short history."""

from dataclasses import replace

from arbitron.clocks import ManualClock
from arbitron.market.backfill import Backfiller
from tests.factories import FIVE_MIN, T0
from tests.integration.conftest import UowFactory
from tests.integration.test_backtesting import at, wave
from tests.integration.test_market_ingestion import FakeVenue

SYMBOL = "BTC/USDT"  # the fake venue's symbol; the venue id is what's under test


async def test_venues_keep_separate_candle_histories(uow: UowFactory) -> None:
    binance = wave(5)
    hyperliquid = [
        replace(c, exchange_id="hyperliquid", close=c.close + 1, high=c.high + 1) for c in binance
    ]
    async with uow() as tx:
        await tx.candles.upsert([*binance, *hyperliquid])
        await tx.commit()

    async with uow() as tx:
        hl = await tx.candles.closed_range("hyperliquid", SYMBOL, FIVE_MIN, start=T0, end=at(5))
        bn = await tx.candles.closed_range("binance", SYMBOL, FIVE_MIN, start=T0, end=at(5))

    assert [c.exchange_id for c in hl] == ["hyperliquid"] * 5
    assert [c.close for c in hl] == [c.close + 1 for c in bn]


async def test_backfill_before_venue_history_reports_the_leading_gap(uow: UowFactory) -> None:
    # Like Hyperliquid's ~5,000-candle cap: the venue only has the last 60 of 100 candles,
    # and answers an early start with its earliest available candles.
    history = [replace(c, exchange_id="hyperliquid") for c in wave(100)[40:]]
    venue = FakeVenue(ManualClock(at(110)), history)
    backfiller = Backfiller(
        market_data=venue,
        uow_factory=uow,
        clock=venue.clock,
        exchange_id="hyperliquid",
        timeframe=FIVE_MIN,
        page_limit=25,
    )

    result = await backfiller.run(SYMBOL, at(0), at(100))

    assert result.written == 60
    assert result.gaps == [(at(0), at(39))]
