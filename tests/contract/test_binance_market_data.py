"""Contract tests: arbitron's Binance market-data adapter over real CCXT parsing."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import ccxt
import pytest

from arbitron.clocks import ManualClock
from arbitron.config.settings import ArbitronConfig
from arbitron.domain.market import Timeframe
from arbitron.exchange.ccxt_adapter import CcxtMarketData, create_market_data, venue_options
from arbitron.exchange.retry import RetryPolicy
from arbitron.ports import errors as venue
from tests.contract.conftest import CannedTransport, binance_with_transport, load_fixture
from tests.unit.test_config import example_data

KLINES = load_fixture("klines_btcusdt_5m.json")
FIRST_OPEN = datetime.fromtimestamp(KLINES[0][0] / 1000, tz=UTC)
LAST_OPEN = datetime.fromtimestamp(KLINES[-1][0] / 1000, tz=UTC)
FIVE_MIN = Timeframe("5m")


def make_config(**exchange: Any) -> ArbitronConfig:
    data = example_data()
    data["exchange"].update(exchange)
    return ArbitronConfig.model_validate(data)


class Harness:
    def __init__(
        self,
        clock: ManualClock,
        symbols: tuple[str, ...] = ("BTC/USDT",),
        request_timeout: timedelta = timedelta(seconds=2),
    ) -> None:
        self.exchange, self.transport = binance_with_transport(dict(venue_options("binance")))
        self.sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        self.clock = clock
        self.adapter = CcxtMarketData(
            self.exchange,
            exchange_id="binance",
            symbols=symbols,
            clock=clock,
            close_grace=timedelta(seconds=5),
            request_timeout=request_timeout,
            retry=RetryPolicy(attempts=3, base_delay=0.5),
            concurrency=2,
            sleep=fake_sleep,
        )


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    # "Now" is just after the last fixture candle opened: it is still open.
    h = Harness(ManualClock(LAST_OPEN + timedelta(minutes=1)))
    yield h
    await h.adapter.close()


class TestMarkets:
    async def test_btc_usdt_metadata_is_exact(self, harness: Harness) -> None:
        markets = await harness.adapter.load_markets()
        btc = markets["BTC/USDT"]

        assert (btc.base, btc.quote, btc.active) == ("BTC", "USDT", True)
        # Values must equal Binance's filter strings exactly (no float noise).
        assert btc.amount_step == Decimal("0.00001")
        assert btc.price_step == Decimal("0.01")
        assert btc.min_amount == Decimal("0.00001")
        assert btc.min_cost == Decimal(5)
        assert harness.adapter.markets_loaded_at == harness.clock.now()

    async def test_only_public_spot_endpoints_are_called(self, harness: Harness) -> None:
        await harness.adapter.load_markets()

        paths = {url.split("?")[0] for url in harness.transport.requests}
        assert paths == {"https://api.binance.com/api/v3/exchangeInfo"}

    async def test_unlisted_symbol_is_refused(self) -> None:
        h = Harness(ManualClock(LAST_OPEN), symbols=("BTC/USDT", "NOPE/USDT"))
        with pytest.raises(venue.MarketUnavailable, match="NOPE/USDT is not listed"):
            await h.adapter.load_markets()
        await h.adapter.close()

    async def test_non_trading_market_is_inactive_not_dropped(self) -> None:
        h = Harness(ManualClock(LAST_OPEN), symbols=("NEO/BTC",))
        markets = await h.adapter.load_markets()
        await h.adapter.close()

        assert markets["NEO/BTC"].active is False


class TestOhlcv:
    async def test_candles_match_raw_klines_exactly(self, harness: Harness) -> None:
        candles = await harness.adapter.fetch_ohlcv("BTC/USDT", FIVE_MIN, limit=12)

        assert len(candles) == len(KLINES)
        for candle, raw in zip(candles, KLINES, strict=True):
            assert candle.open_time == datetime.fromtimestamp(raw[0] / 1000, tz=UTC)
            assert (candle.open, candle.high, candle.low, candle.close, candle.volume) == tuple(
                Decimal(v) for v in raw[1:6]
            )

    async def test_closedness_respects_grace_period(self, harness: Harness) -> None:
        candles = await harness.adapter.fetch_ohlcv("BTC/USDT", FIVE_MIN)
        assert [c.is_closed for c in candles] == [True] * 11 + [False]

        # Exactly at close time the last candle is still within the grace period.
        harness.clock.set(LAST_OPEN + timedelta(minutes=5))
        candles = await harness.adapter.fetch_ohlcv("BTC/USDT", FIVE_MIN)
        assert not candles[-1].is_closed

        harness.clock.set(LAST_OPEN + timedelta(minutes=5, seconds=5))
        candles = await harness.adapter.fetch_ohlcv("BTC/USDT", FIVE_MIN)
        assert candles[-1].is_closed

    async def test_since_is_sent_as_epoch_millis(self, harness: Harness) -> None:
        await harness.adapter.fetch_ohlcv("BTC/USDT", FIVE_MIN, since=FIRST_OPEN, limit=12)

        request = harness.transport.requests[-1]
        assert f"startTime={KLINES[0][0]}" in request
        assert "interval=5m" in request
        assert "limit=12" in request

    async def test_symbol_outside_allowlist_is_refused_before_any_request(
        self, harness: Harness
    ) -> None:
        with pytest.raises(venue.MarketUnavailable, match="allowlist"):
            await harness.adapter.fetch_ohlcv("ETH/USDT", FIVE_MIN)
        assert harness.transport.requests == []

    async def test_ohlc_invariant_violation_is_invalid_venue_data(self, harness: Harness) -> None:
        bad = [list(row) for row in KLINES]
        bad[3][2] = "1.00"  # high far below open/close
        harness.transport.overrides["/api/v3/klines"] = lambda: bad

        with pytest.raises(venue.InvalidVenueData, match="OHLC invariant"):
            await harness.adapter.fetch_ohlcv("BTC/USDT", FIVE_MIN)


class TestTickerAndTime:
    async def test_ticker(self, harness: Harness) -> None:
        raw = load_fixture("ticker_24hr_btcusdt.json")
        ticker = await harness.adapter.fetch_ticker("BTC/USDT")

        assert ticker.bid == Decimal(raw["bidPrice"])
        assert ticker.ask == Decimal(raw["askPrice"])
        assert ticker.last == Decimal(raw["lastPrice"])

    async def test_server_time(self, harness: Harness) -> None:
        raw = load_fixture("time.json")

        assert await harness.adapter.fetch_time() == datetime.fromtimestamp(
            raw["serverTime"] / 1000, tz=UTC
        )


class TestFailures:
    @staticmethod
    def fail_with(transport: CannedTransport, path: str, *errors: BaseException) -> None:
        pending = list(errors)

        def respond() -> Any:
            if pending:
                raise pending.pop(0)
            return load_fixture("time.json")

        transport.overrides[path] = respond

    @pytest.mark.parametrize(
        ("raised", "expected"),
        [
            (ccxt.RequestTimeout("t"), venue.RequestTimeout),
            (ccxt.RateLimitExceeded("r"), venue.RateLimited),
            (ccxt.DDoSProtection("418"), venue.RateLimited),
            (ccxt.OnMaintenance("m"), venue.ExchangeUnavailable),
            (ccxt.NetworkError("n"), venue.NetworkTransient),
        ],
    )
    async def test_transient_errors_retry_then_succeed(
        self, harness: Harness, raised: BaseException, expected: type[venue.VenueError]
    ) -> None:
        assert expected.retryable
        self.fail_with(harness.transport, "/api/v3/time", raised, raised)

        await harness.adapter.fetch_time()

        assert len(harness.transport.requests) == 3
        assert len(harness.sleeps) == 2
        assert harness.sleeps[1] > harness.sleeps[0]  # exponential backoff

    async def test_transient_errors_give_up_after_bounded_attempts(self, harness: Harness) -> None:
        self.fail_with(harness.transport, "/api/v3/time", *[ccxt.RequestTimeout("t")] * 5)

        with pytest.raises(venue.RequestTimeout) as exc_info:
            await harness.adapter.fetch_time()

        assert isinstance(exc_info.value.__cause__, ccxt.RequestTimeout)
        assert len(harness.transport.requests) == 3

    @pytest.mark.parametrize(
        ("raised", "expected"),
        [
            (ccxt.AuthenticationError("a"), venue.AuthenticationFailure),
            (ccxt.RestrictedLocation("geo"), venue.AuthenticationFailure),
            (ccxt.BadSymbol("s"), venue.MarketUnavailable),
            (ccxt.BadResponse("garbled"), venue.UnknownExchangeError),
        ],
    )
    async def test_permanent_errors_are_not_retried(
        self, harness: Harness, raised: BaseException, expected: type[venue.VenueError]
    ) -> None:
        self.fail_with(harness.transport, "/api/v3/time", raised)

        with pytest.raises(expected):
            await harness.adapter.fetch_time()

        assert len(harness.transport.requests) == 1
        assert harness.sleeps == []

    async def test_hung_call_is_cut_off_by_outer_timeout(self) -> None:
        h = Harness(ManualClock(LAST_OPEN), request_timeout=timedelta(milliseconds=30))

        async def hang(*_args: Any, **_kwargs: Any) -> Any:
            await asyncio.sleep(10)

        h.exchange.fetch = hang  # type: ignore[method-assign]
        with pytest.raises(venue.RequestTimeout):
            await h.adapter.fetch_time()
        assert len(h.sleeps) == 2  # timeouts are retryable reads
        await h.adapter.close()

    async def test_concurrency_is_bounded(self, harness: Harness) -> None:
        active = 0
        peak = 0

        async def slow(*_args: Any, **_kwargs: Any) -> Any:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return load_fixture("time.json")

        harness.exchange.fetch = slow  # type: ignore[method-assign]
        await asyncio.gather(*(harness.adapter.fetch_time() for _ in range(8)))

        assert peak == 2


class TestFactory:
    def test_creates_public_only_binance_adapter(self) -> None:
        adapter = create_market_data(make_config(), ManualClock(LAST_OPEN))
        exchange = adapter._exchange  # pyright: ignore[reportPrivateUsage]

        assert exchange.id == "binance"
        assert not getattr(exchange, "apiKey", None)
        assert not getattr(exchange, "secret", None)
        assert exchange.options["fetchMarkets"]["types"] == ["spot"]  # type: ignore[attr-defined]
        assert exchange.timeout == 10000  # type: ignore[attr-defined]
        assert exchange.enableRateLimit is True  # type: ignore[attr-defined]

    def test_unsupported_venue_is_refused(self) -> None:
        with pytest.raises(venue.UnsupportedCapability, match="not supported"):
            create_market_data(make_config(id="kraken"), ManualClock(LAST_OPEN))

    def test_sandbox_mode_switches_endpoints(self) -> None:
        data = example_data()
        data["app"]["mode"] = "sandbox"
        data["exchange"]["sandbox"] = True
        adapter = create_market_data(ArbitronConfig.model_validate(data), ManualClock(LAST_OPEN))
        exchange: Any = adapter._exchange  # pyright: ignore[reportPrivateUsage]

        assert "testnet" in str(exchange.urls["api"]["public"])
