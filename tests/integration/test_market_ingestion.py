"""Collector, quality gate, and service against real PostgreSQL with a fake venue."""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from arbitron.clocks import ManualClock
from arbitron.config.settings import ArbitronConfig
from arbitron.domain.enums import MarketType
from arbitron.domain.market import Candle, Market, Ticker, Timeframe
from arbitron.market.collector import CandleCollector
from arbitron.market.freshness import DataIssueCode, MarketDataMonitor, QualityThresholds
from arbitron.market.service import MarketDataService
from arbitron.ports.errors import InvalidVenueData, NetworkTransient, VenueError
from tests.factories import FIVE_MIN, T0, make_candle
from tests.integration.conftest import UowFactory
from tests.unit.test_config import example_data

C = DataIssueCode
SYMBOL = "BTC/USDT"
MARKET = Market(
    "binance",
    SYMBOL,
    "BTC",
    "USDT",
    MarketType.SPOT,
    Decimal("0.00001"),
    Decimal("0.01"),
    Decimal("0.00001"),
    Decimal(5),
    active=True,
)


class FakeVenue:
    """Serves a canonical candle history; closedness follows the shared clock."""

    def __init__(self, clock: ManualClock, history: Sequence[Candle]) -> None:
        self.clock = clock
        self.history = list(history)
        self.requests: list[tuple[datetime | None, int | None]] = []
        self.fail_next: list[Exception] = []
        self.market = MARKET
        self.time_offset = timedelta(0)
        self.ticker: Ticker | None = None  # overrides the default touch
        self.ticker_failures: list[Exception] = []

    async def load_markets(self) -> Mapping[str, Market]:
        return {SYMBOL: self.market}

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Sequence[Candle]:
        self.requests.append((since, limit))
        if self.fail_next:
            raise self.fail_next.pop(0)
        now = self.clock.now()
        visible = [c for c in self.history if c.open_time <= now]
        if since is not None:
            visible = [c for c in visible if c.open_time >= since]
        visible = visible[:limit] if limit else visible
        return [replace(c, is_closed=c.close_time <= now, ingested_at=now) for c in visible]

    async def fetch_ticker(self, symbol: str) -> Ticker:
        """Default touch: bid at the latest visible close, ask one tick above."""
        if self.ticker_failures:
            raise self.ticker_failures.pop(0)
        if self.ticker is not None:
            return replace(self.ticker, timestamp=self.clock.now())
        now = self.clock.now()
        last = [c for c in self.history if c.close_time <= now][-1]
        return Ticker("binance", symbol, last.close, last.close + Decimal("0.01"), last.close, now)

    async def fetch_time(self) -> datetime:
        return self.clock.now() + self.time_offset

    async def close(self) -> None:
        return None


def history(count: int, start: datetime = T0) -> list[Candle]:
    return [
        make_candle(
            open_time=start + i * FIVE_MIN.duration,
            ingested_at=start + (i + 1) * FIVE_MIN.duration,
            close=Decimal(100 + i % 7),
            high=Decimal(120),
            low=Decimal(90),
        )
        for i in range(count)
    ]


def collector(venue: FakeVenue, uow: UowFactory, **kw: int) -> CandleCollector:
    return CandleCollector(
        market_data=venue,
        uow_factory=uow,
        clock=venue.clock,
        exchange_id="binance",
        timeframe=FIVE_MIN,
        backfill_candles=kw.get("backfill", 50),
        page_limit=kw.get("page_limit", 1000),
        max_pages=kw.get("max_pages", 20),
    )


def monitor(venue: FakeVenue, uow: UowFactory) -> MarketDataMonitor:
    return MarketDataMonitor(
        exchange_id="binance",
        timeframe=FIVE_MIN,
        uow_factory=uow,
        clock=venue.clock,
        thresholds=QualityThresholds(
            max_candle_age=timedelta(minutes=10),
            metadata_max_age=timedelta(hours=1),
            max_clock_skew=timedelta(seconds=1),
        ),
    )


async def system_event_types(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT event_type FROM system_events ORDER BY id"))
        return [row[0] for row in rows]


# "Now" is 2 minutes into candle 100 (open), so candles 0..99 are closed.
NOW = T0 + 100 * FIVE_MIN.duration + timedelta(minutes=2)


class TestCollector:
    async def test_first_sync_backfills_closed_candles_only(self, uow: UowFactory) -> None:
        venue = FakeVenue(ManualClock(NOW), history(200))

        result = await collector(venue, uow, backfill=50).sync(SYMBOL)

        assert result.written == 50
        assert result.issues == ()
        assert result.latest_closed == T0 + 99 * FIVE_MIN.duration
        assert venue.requests[0][0] == T0 + 50 * FIVE_MIN.duration

    async def test_incremental_sync_overlaps_one_candle(self, uow: UowFactory) -> None:
        clock = ManualClock(NOW)
        venue = FakeVenue(clock, history(200))
        c = collector(venue, uow)
        await c.sync(SYMBOL)

        clock.advance(timedelta(minutes=15))
        result = await c.sync(SYMBOL)

        assert venue.requests[-1][0] == T0 + 99 * FIVE_MIN.duration
        assert result.written == 3
        assert result.latest_closed == T0 + 102 * FIVE_MIN.duration

    async def test_paginates_and_reports_incomplete_backfill(self, uow: UowFactory) -> None:
        venue = FakeVenue(ManualClock(NOW), history(200))

        result = await collector(venue, uow, backfill=100, page_limit=10, max_pages=3).sync(SYMBOL)

        assert result.written == 30
        assert [i.code for i in result.issues] == [C.BACKFILL_INCOMPLETE]

        # The next sync continues where the previous one stopped.
        result = await collector(venue, uow, page_limit=10, max_pages=20).sync(SYMBOL)
        assert result.latest_closed == T0 + 99 * FIVE_MIN.duration
        assert result.issues == ()

    async def test_revised_closed_candle_is_kept_and_reported(
        self, uow: UowFactory, engine: AsyncEngine
    ) -> None:
        clock = ManualClock(NOW)
        venue = FakeVenue(clock, history(200))
        c = collector(venue, uow)
        await c.sync(SYMBOL)
        venue.history[99] = replace(venue.history[99], close=Decimal(119))

        clock.advance(timedelta(minutes=5))
        result = await c.sync(SYMBOL)

        assert [i.code for i in result.issues] == [C.CANDLE_REVISED]
        async with uow() as tx:
            stored = await tx.candles.closed_range(
                "binance",
                SYMBOL,
                FIVE_MIN,
                start=T0 + 99 * FIVE_MIN.duration,
                end=T0 + 100 * FIVE_MIN.duration,
            )
        assert stored[0].close == history(200)[99].close
        assert await system_event_types(engine) == ["market_data.candle_revised"]

    async def test_venue_gap_is_persisted_around_and_reported(self, uow: UowFactory) -> None:
        candles = history(200)
        del candles[70:73]
        venue = FakeVenue(ManualClock(NOW), candles)

        result = await collector(venue, uow).sync(SYMBOL)

        assert result.written == 47
        assert [i.code for i in result.issues] == [C.GAP]
        assert "3 interval(s)" in result.issues[0].detail

    async def test_invalid_series_is_not_persisted(
        self, uow: UowFactory, engine: AsyncEngine
    ) -> None:
        candles = history(200)
        candles.insert(80, candles[80])  # venue returns a duplicate
        venue = FakeVenue(ManualClock(NOW), candles)

        result = await collector(venue, uow).sync(SYMBOL)

        assert result.written == 0
        assert {i.code for i in result.issues} == {C.INVALID_SERIES}
        async with uow() as tx:
            assert await tx.candles.latest_closed("binance", SYMBOL, FIVE_MIN) is None
        assert await system_event_types(engine) == ["market_data.invalid_series"]

    async def test_invalid_venue_data_is_recorded_not_raised(
        self, uow: UowFactory, engine: AsyncEngine
    ) -> None:
        venue = FakeVenue(ManualClock(NOW), history(200))
        venue.fail_next = [InvalidVenueData("BTC/USDT ohlcv[3]: OHLC invariant violated")]

        result = await collector(venue, uow).sync(SYMBOL)

        assert result.written == 0
        assert [i.code for i in result.issues] == [C.VENUE_DATA_INVALID]
        assert await system_event_types(engine) == ["market_data.venue_data_invalid"]

    async def test_transient_venue_errors_propagate(self, uow: UowFactory) -> None:
        venue = FakeVenue(ManualClock(NOW), history(200))
        venue.fail_next = [NetworkTransient("down")]

        with pytest.raises(NetworkTransient):
            await collector(venue, uow).sync(SYMBOL)


class TestQualityGate:
    async def test_fresh_complete_data_passes(self, uow: UowFactory) -> None:
        venue = FakeVenue(ManualClock(NOW), history(200))
        await collector(venue, uow, backfill=50).sync(SYMBOL)
        gate = monitor(venue, uow)
        await gate.refresh(venue)

        report = await gate.evaluate(SYMBOL, lookback=50)

        assert report.ok, report.issues

    async def test_blocks_before_metadata_and_clock_are_known(self, uow: UowFactory) -> None:
        venue = FakeVenue(ManualClock(NOW), history(200))
        await collector(venue, uow).sync(SYMBOL)

        report = await monitor(venue, uow).evaluate(SYMBOL, lookback=10)

        assert not report.ok
        assert {i.code for i in report.issues} == {C.MARKET_METADATA_MISSING, C.CLOCK_UNVERIFIED}

    async def test_blocks_when_history_shorter_than_lookback(self, uow: UowFactory) -> None:
        venue = FakeVenue(ManualClock(NOW), history(200))
        await collector(venue, uow, backfill=50).sync(SYMBOL)
        gate = monitor(venue, uow)
        await gate.refresh(venue)

        report = await gate.evaluate(SYMBOL, lookback=60)

        assert [i.code for i in report.issues] == [C.INCOMPLETE_LOOKBACK]

    async def test_blocks_when_data_goes_stale(self, uow: UowFactory) -> None:
        clock = ManualClock(NOW)
        venue = FakeVenue(clock, history(200))
        await collector(venue, uow).sync(SYMBOL)
        gate = monitor(venue, uow)
        await gate.refresh(venue)

        clock.advance(timedelta(minutes=10))
        report = await gate.evaluate(SYMBOL, lookback=10)

        assert [i.code for i in report.issues] == [C.STALE]

    async def test_blocks_on_clock_skew_and_inactive_market(self, uow: UowFactory) -> None:
        venue = FakeVenue(ManualClock(NOW), history(200))
        venue.time_offset = timedelta(seconds=3)
        venue.market = replace(MARKET, active=False)
        await collector(venue, uow).sync(SYMBOL)
        gate = monitor(venue, uow)
        await gate.refresh(venue)

        report = await gate.evaluate(SYMBOL, lookback=10)

        assert {i.code for i in report.issues} == {C.CLOCK_SKEW, C.MARKET_INACTIVE}

    async def test_failed_refresh_keeps_metadata_that_then_ages(self, uow: UowFactory) -> None:
        clock = ManualClock(NOW)
        venue = FakeVenue(clock, history(400))
        c = collector(venue, uow)
        gate = monitor(venue, uow)
        await gate.refresh(venue)

        async def broken() -> Mapping[str, Market]:
            raise NetworkTransient("down")

        venue.load_markets = broken  # type: ignore[method-assign]
        clock.advance(timedelta(hours=1, minutes=5))
        with pytest.raises(NetworkTransient):
            await gate.refresh(venue)
        await c.sync(SYMBOL)

        report = await gate.evaluate(SYMBOL, lookback=10)
        assert [i.code for i in report.issues] == [C.MARKET_METADATA_STALE]


class TestService:
    def make_service(
        self, venue: FakeVenue, uow: UowFactory, lookback: int = 20
    ) -> MarketDataService:
        data = example_data()
        data["market"] = {"backfill_candles": 50}
        return MarketDataService(
            ArbitronConfig.model_validate(data),
            market_data=venue,
            uow_factory=uow,
            clock=venue.clock,
            lookback=lookback,
        )

    async def test_cycle_refreshes_syncs_and_evaluates(self, uow: UowFactory) -> None:
        venue = FakeVenue(ManualClock(NOW), history(200))

        outcome = await self.make_service(venue, uow).run_cycle()

        assert outcome.ok
        assert outcome.results[0].written == 50

    def test_wakes_after_next_close_plus_grace(self, uow: UowFactory) -> None:
        venue = FakeVenue(ManualClock(NOW), history(200))

        # NOW is 2 minutes into a 5m candle; grace is 5 seconds.
        assert self.make_service(venue, uow).seconds_until_next_close() == 185.0

    async def test_follow_survives_venue_errors_and_stops(self, uow: UowFactory) -> None:
        venue = FakeVenue(ManualClock(NOW), history(200))
        venue.fail_next = [NetworkTransient("blip")]
        service = self.make_service(venue, uow)
        stop = asyncio.Event()
        outcomes: list[bool] = []
        errors: list[VenueError] = []

        def on_cycle(outcome: object) -> None:
            outcomes.append(True)
            stop.set()

        def on_error(exc: VenueError) -> None:
            errors.append(exc)

        service.seconds_until_next_close = lambda: 0.0  # type: ignore[method-assign]
        await asyncio.wait_for(service.follow(on_cycle, on_error, stop=stop), timeout=5)

        assert len(errors) == 1
        assert outcomes == [True]
