import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import ccxt
import pytest

from arbitron.exchange.capabilities import VenueCapabilities
from arbitron.exchange.errors import translate, venue_errors
from arbitron.exchange.mapping import (
    DECIMAL_PLACES,
    SIGNIFICANT_DIGITS,
    TICK_SIZE,
    candles_from_ohlcv,
    datetime_from_ms,
    datetime_to_ms,
    decimal_from_ccxt,
    market_from_ccxt,
)
from arbitron.exchange.retry import RetryPolicy, retry_read
from arbitron.ports import errors as venue
from tests.factories import FIVE_MIN, T0


class TestDecimalFromCcxt:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1e-05, "0.00001"),
            (0.1, "0.1"),
            (83134.01, "83134.01"),
            (13.87238, "13.87238"),
            (9000.0, "9000.0"),
            ("0.00001000", "0.00001000"),
            (5, "5"),
        ],
    )
    def test_recovers_exact_decimal(self, value: object, expected: str) -> None:
        assert decimal_from_ccxt(value, "x") == Decimal(expected)

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), "abc", None, True, [1]])
    def test_rejects_invalid(self, value: object) -> None:
        with pytest.raises(venue.InvalidVenueData):
            decimal_from_ccxt(value, "x")

    def test_epoch_millis_round_trip(self) -> None:
        moment = datetime(2026, 10, 11, 15, 30, tzinfo=UTC)

        assert datetime_from_ms(datetime_to_ms(moment), "t") == moment
        with pytest.raises(venue.InvalidVenueData):
            datetime_from_ms("1700000000000", "t")


def raw_market(**overrides: object) -> dict[str, object]:
    market: dict[str, object] = {
        "symbol": "BTC/USDT",
        "base": "BTC",
        "quote": "USDT",
        "spot": True,
        "active": True,
        "precision": {"amount": 1e-05, "price": 0.01},
        "limits": {"amount": {"min": 1e-05}, "cost": {"min": 5.0}},
    }
    market.update(overrides)
    return market


class TestMarketFromCcxt:
    def test_tick_size_mode(self) -> None:
        market = market_from_ccxt(raw_market(), exchange_id="binance", precision_mode=TICK_SIZE)

        assert market.amount_step == Decimal("0.00001")
        assert market.min_cost == Decimal(5)

    def test_decimal_places_mode(self) -> None:
        raw = raw_market(precision={"amount": 5, "price": 2})
        market = market_from_ccxt(raw, exchange_id="x", precision_mode=DECIMAL_PLACES)

        assert market.amount_step == Decimal("0.00001")
        assert market.price_step == Decimal("0.01")

    def test_significant_digits_mode_is_refused(self) -> None:
        with pytest.raises(venue.InvalidVenueData, match="precision mode"):
            market_from_ccxt(raw_market(), exchange_id="x", precision_mode=SIGNIFICANT_DIGITS)

    def test_missing_limits_default_to_zero(self) -> None:
        market = market_from_ccxt(
            raw_market(limits={}), exchange_id="binance", precision_mode=TICK_SIZE
        )

        assert market.min_amount == 0
        assert market.min_cost == 0

    @pytest.mark.parametrize(
        "overrides",
        [
            {"spot": False},
            {"precision": {"amount": None, "price": 0.01}},
            {"precision": {"amount": 0, "price": 0.01}},
            {"symbol": "ETH/USDT"},
        ],
    )
    def test_invalid_metadata(self, overrides: dict[str, object]) -> None:
        with pytest.raises(venue.InvalidVenueData):
            market_from_ccxt(
                raw_market(**overrides), exchange_id="binance", precision_mode=TICK_SIZE
            )


class TestCandlesFromOhlcv:
    def test_short_row_is_invalid(self) -> None:
        with pytest.raises(venue.InvalidVenueData, match="expected 6 fields"):
            candles_from_ohlcv(
                [[datetime_to_ms(T0), 1, 2, 1]],
                exchange_id="binance",
                symbol="BTC/USDT",
                timeframe=FIVE_MIN,
                now=T0,
                close_grace=timedelta(0),
            )


class TestErrorTranslation:
    @pytest.mark.parametrize(
        ("raised", "expected"),
        [
            (ccxt.RequestTimeout(), venue.RequestTimeout),
            (ccxt.RateLimitExceeded(), venue.RateLimited),
            (ccxt.DDoSProtection(), venue.RateLimited),
            (ccxt.OnMaintenance(), venue.ExchangeUnavailable),
            (ccxt.ExchangeNotAvailable(), venue.ExchangeUnavailable),
            (ccxt.InvalidNonce(), venue.NetworkTransient),
            (ccxt.NetworkError(), venue.NetworkTransient),
            (ccxt.DuplicateOrderId(), venue.UnknownExchangeError),
            (ccxt.OrderNotFound(), venue.InvalidOrder),
            (ccxt.InvalidOrder(), venue.InvalidOrder),
            (ccxt.InsufficientFunds(), venue.InsufficientFunds),
            (ccxt.PermissionDenied(), venue.AuthenticationFailure),
            (ccxt.AccountSuspended(), venue.AuthenticationFailure),
            (ccxt.RestrictedLocation(), venue.AuthenticationFailure),
            (ccxt.BadSymbol(), venue.MarketUnavailable),
            (ccxt.MarketClosed(), venue.MarketUnavailable),
            (ccxt.NotSupported(), venue.UnsupportedCapability),
            (ccxt.BadRequest(), venue.UnknownExchangeError),
            (ccxt.NullResponse(), venue.UnknownExchangeError),
            (ccxt.ExchangeError(), venue.UnknownExchangeError),
            (TimeoutError(), venue.RequestTimeout),
        ],
    )
    def test_mapping(self, raised: BaseException, expected: type[venue.VenueError]) -> None:
        translated = translate(raised)

        assert type(translated) is expected

    def test_non_venue_errors_pass_through(self) -> None:
        assert translate(KeyError("x")) is None
        with pytest.raises(KeyError), venue_errors():
            raise KeyError("x")

    def test_context_manager_chains_original(self) -> None:
        with pytest.raises(venue.RateLimited) as exc_info, venue_errors():
            raise ccxt.RateLimitExceeded("slow down")

        assert isinstance(exc_info.value.__cause__, ccxt.RateLimitExceeded)
        assert "slow down" in str(exc_info.value)


class TestCapabilities:
    def test_only_native_support_counts(self) -> None:
        caps = VenueCapabilities.from_ccxt_has(
            {"fetchOHLCV": True, "fetchTicker": "emulated", "fetchTime": None}
        )

        assert caps.fetch_ohlcv
        assert not caps.fetch_ticker
        with pytest.raises(venue.UnsupportedCapability, match="fetch_ticker, fetch_time"):
            caps.require_market_data()


class TestRetry:
    def test_delays_grow_and_are_capped(self) -> None:
        policy = RetryPolicy(attempts=10, base_delay=1, max_delay=4, jitter=0)
        rng = random.Random(0)

        assert [policy.delay(n, rng) for n in range(1, 6)] == [1, 2, 4, 4, 4]

    def test_jitter_stays_within_bounds(self) -> None:
        policy = RetryPolicy(base_delay=1, jitter=0.25)
        rng = random.Random(42)

        assert all(0.75 <= policy.delay(1, rng) <= 1.25 for _ in range(200))

    @pytest.mark.parametrize("kwargs", [{"attempts": 0}, {"jitter": 1.0}, {"jitter": -0.1}])
    def test_invalid_policy(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(ValueError):  # noqa: PT011
            RetryPolicy(**kwargs)  # type: ignore[arg-type]

    async def test_non_venue_errors_are_never_retried(self) -> None:
        calls = 0

        async def boom() -> None:
            nonlocal calls
            calls += 1
            raise KeyError("bug")

        with pytest.raises(KeyError):
            await retry_read(boom, RetryPolicy(attempts=5))
        assert calls == 1
