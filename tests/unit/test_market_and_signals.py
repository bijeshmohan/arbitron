from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from arbitron.domain.enums import MarketType, Side
from arbitron.domain.errors import DomainValidationError
from arbitron.domain.market import Market, Ticker, Timeframe
from tests.factories import SMA_CONFIG, T0, at, make_candle, make_signal


class TestTimeframe:
    @pytest.mark.parametrize(
        ("value", "duration"),
        [
            ("1m", timedelta(minutes=1)),
            ("5m", timedelta(minutes=5)),
            ("4h", timedelta(hours=4)),
            ("1d", timedelta(days=1)),
            ("1w", timedelta(weeks=1)),
        ],
    )
    def test_duration(self, value: str, duration: timedelta) -> None:
        assert Timeframe(value).duration == duration
        assert str(Timeframe(value)) == value

    @pytest.mark.parametrize("value", ["", "5", "0m", "1M", "1y", "m5", "05m"])
    def test_invalid(self, value: str) -> None:
        with pytest.raises(DomainValidationError, match="invalid timeframe"):
            Timeframe(value)


class TestCandle:
    def test_valid_candle(self) -> None:
        candle = make_candle()

        assert candle.close_time == at(5)
        assert candle.identity == ("binance", "BTC/USDT", "5m", T0)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"high": Decimal(104)},  # high below close
            {"low": Decimal(101)},  # low above open
            {"high": Decimal(90)},  # high below low
        ],
    )
    def test_ohlc_invariants(self, overrides: dict[str, Decimal]) -> None:
        with pytest.raises(DomainValidationError, match="OHLC invariant"):
            make_candle(**overrides)

    @pytest.mark.parametrize("field", ["open", "high", "low", "close"])
    def test_prices_must_be_positive(self, field: str) -> None:
        with pytest.raises(DomainValidationError):
            make_candle(**{field: Decimal(0)})

    def test_negative_volume_is_refused(self) -> None:
        with pytest.raises(DomainValidationError):
            make_candle(volume=Decimal(-1))

    def test_zero_volume_is_allowed(self) -> None:
        assert make_candle(volume=Decimal(0)).volume == 0

    def test_cannot_be_closed_before_close_time(self) -> None:
        with pytest.raises(DomainValidationError, match="before its close time"):
            make_candle(ingested_at=at(4))

    def test_open_candle_can_be_ingested_early(self) -> None:
        assert not make_candle(is_closed=False, ingested_at=at(2)).is_closed


class TestMarket:
    def _make(self, **overrides: Any) -> Market:
        fields: dict[str, Any] = {
            "exchange_id": "binance",
            "symbol": "BTC/USDT",
            "base": "BTC",
            "quote": "USDT",
            "market_type": MarketType.SPOT,
            "amount_step": Decimal("0.00001"),
            "price_step": Decimal("0.01"),
            "min_amount": Decimal("0.00001"),
            "min_cost": Decimal(5),
            "active": True,
        }
        fields.update(overrides)
        return Market(**fields)

    def test_valid(self) -> None:
        assert self._make().active

    def test_symbol_must_match_base_quote(self) -> None:
        with pytest.raises(DomainValidationError, match="does not match"):
            self._make(symbol="ETH/USDT")

    def test_step_must_be_positive(self) -> None:
        with pytest.raises(DomainValidationError):
            self._make(amount_step=Decimal(0))


class TestTicker:
    def test_crossed_book_is_refused(self) -> None:
        with pytest.raises(DomainValidationError, match="crossed"):
            Ticker("binance", "BTC/USDT", Decimal(101), Decimal(100), None, T0)

    def test_missing_sides_allowed(self) -> None:
        assert Ticker("binance", "BTC/USDT", None, None, Decimal(100), T0).bid is None


class TestSignal:
    def test_id_is_deterministic_from_dedupe_key(self) -> None:
        assert make_signal().id == make_signal().id
        assert make_signal().dedupe_key == (
            f"sma_cross:1:{SMA_CONFIG.config_hash}:binance:BTC/USDT:5m:2026-01-01T12:00:00+00:00"
        )

    def test_side_and_rationale_do_not_change_identity(self) -> None:
        # At most one signal per strategy/version/market/timeframe/candle.
        other = make_signal(side=Side.SELL, reason_code="other", explanation="x")

        assert other.id == make_signal().id

    @pytest.mark.parametrize(
        "overrides",
        [
            {"strategy_version": "2"},
            {"config_hash": "f" * 64},
            {"symbol": "ETH/USDT"},
            {"timeframe": Timeframe("1h")},
            {"source_candle_time": at(5)},
        ],
    )
    def test_identity_changes_with_key_fields(self, overrides: dict[str, Any]) -> None:
        assert make_signal(**overrides).id != make_signal().id

    def test_metadata_is_copied_and_read_only(self) -> None:
        metadata = {"fast": "10"}
        signal = make_signal(metadata=metadata)
        metadata["fast"] = "99"

        assert signal.metadata["fast"] == "10"
        with pytest.raises(TypeError):
            signal.metadata["fast"] = "1"  # type: ignore[index]

    def test_empty_reason_is_refused(self) -> None:
        with pytest.raises(DomainValidationError, match="reason_code"):
            make_signal(reason_code="  ")
