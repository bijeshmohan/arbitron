"""Fill model, position accounting, and signal freshness (hand-calculated)."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from arbitron.domain.enums import OrderType, Side, TimeInForce
from arbitron.domain.fills import Fee, Fill
from arbitron.domain.positions import Position
from arbitron.execution.freshness import stale_reason
from arbitron.portfolio.accounting import AccountingError, apply_fill
from arbitron.simulation.fill_model import FillModel, SimulatedFill, UnsupportedOrderError
from tests.factories import FIVE_MIN, T0, make_market, make_signal, make_ticker, make_trade

MARKET = make_market()  # lot 0.001, tick 0.01, min 0.001 / 5 USDT
MODEL = FillModel(slippage_bps=Decimal(5), fee_rate=Decimal("0.001"))


def fill(model: FillModel = MODEL, **overrides: Any) -> SimulatedFill:
    fields: dict[str, Any] = {
        "side": Side.BUY,
        "order_type": OrderType.LIMIT,
        "time_in_force": TimeInForce.IOC,
        "amount": Decimal("0.249"),
        "limit_price": Decimal("100.11"),
        "ticker": make_ticker(bid="100.00", ask="100.01"),
        "market": MARKET,
    }
    fields.update(overrides)
    return model.fill(**fields)


class TestFillModel:
    def test_buy_fills_at_ask_plus_slippage_rounded_up(self) -> None:
        result = fill()

        # 100.01 * 1.0005 = 100.060005 -> tick up -> 100.07 (<= limit 100.11)
        assert result.price == Decimal("100.07")
        assert result.filled == Decimal("0.249")
        # fee in base: 0.249 * 0.001
        assert (result.fee, result.fee_currency) == (Decimal("0.000249"), "BTC")

    def test_sell_fills_at_bid_minus_slippage_rounded_down(self) -> None:
        result = fill(side=Side.SELL, amount=Decimal("0.248"), limit_price=Decimal("99.90"))

        # 100.00 * 0.9995 = 99.95 (on tick); fee in quote: 0.248 * 99.95 * 0.001
        assert result.price == Decimal("99.95")
        assert result.fee == Decimal("0.0247876")
        assert result.fee_currency == "USDT"

    @pytest.mark.parametrize(("side", "limit"), [(Side.BUY, "100.06"), (Side.SELL, "99.96")])
    def test_ioc_expires_when_slipped_price_breaches_limit(self, side: Side, limit: str) -> None:
        result = fill(side=side, limit_price=Decimal(limit))

        assert not result.is_fill
        assert result.price is None

    def test_partial_fill_ratio_rounds_down_to_lot(self) -> None:
        partial = replace(MODEL, fill_ratio=Decimal("0.5"))

        result = fill(partial)

        assert result.filled == Decimal("0.124")  # 0.1245 -> lot

    def test_zero_slippage_fills_at_touch(self) -> None:
        result = fill(replace(MODEL, slippage_bps=Decimal(0)))

        assert result.price == Decimal("100.01")

    def test_missing_quote_side_expires(self) -> None:
        from arbitron.domain.market import Ticker  # noqa: PLC0415

        one_sided = Ticker("binance", "BTC/USDT", Decimal(100), None, None, T0)
        assert not fill(ticker=one_sided).is_fill

    @pytest.mark.parametrize(
        "overrides",
        [{"order_type": OrderType.MARKET, "limit_price": None}, {"time_in_force": TimeInForce.GTC}],
    )
    def test_only_ioc_limits_are_modeled(self, overrides: dict[str, Any]) -> None:
        with pytest.raises(UnsupportedOrderError):
            fill(**overrides)

    @pytest.mark.parametrize(
        "kwargs",
        [{"slippage_bps": Decimal(-1)}, {"fee_rate": Decimal(-1)}, {"fill_ratio": Decimal(0)}],
    )
    def test_invalid_model(self, kwargs: dict[str, Decimal]) -> None:
        params = {"slippage_bps": Decimal(5), "fee_rate": Decimal("0.001")} | kwargs
        with pytest.raises(ValueError):  # noqa: PT011
            FillModel(**params)

    def test_deterministic(self) -> None:
        assert fill() == fill()


def trade_fill(side: Side, amount: str, price: str, fee: str, currency: str) -> Fill:
    from tests.factories import make_intent  # noqa: PLC0415

    trade = make_trade(
        side=side,
        amount=Decimal(amount),
        price=Decimal(price),
        fee=Fee(Decimal(fee), currency),
        trade_id=f"t-{side}-{amount}-{price}",
    )
    return Fill(make_intent().id, trade)


class TestAccounting:
    def test_buy_with_base_fee_raises_average_cost(self) -> None:
        position = apply_fill(
            None, trade_fill(Side.BUY, "0.249", "100.07", "0.000249", "BTC"), MARKET
        )

        # received 0.248751 BTC for 24.91743 USDT -> avg 100.170170...
        assert position.quantity == Decimal("0.248751")
        assert position.average_cost == Decimal("100.170170170170170170")
        assert position.fees_paid == Decimal("0.02491743")  # 0.000249 BTC at 100.07
        assert position.realized_pnl == 0

    def test_round_trip_realizes_pnl_and_leaves_dust(self) -> None:
        bought = apply_fill(
            None, trade_fill(Side.BUY, "0.249", "100.07", "0.000249", "BTC"), MARKET
        )
        sold = apply_fill(
            bought, trade_fill(Side.SELL, "0.248", "102.00", "0.025296", "USDT"), MARKET
        )

        # proceeds 25.296 - 0.025296 = 25.270704; cost 0.248 x stored avg = 24.842202202...
        assert sold.quantity == Decimal("0.000751")
        assert sold.average_cost == bought.average_cost  # unchanged by a sale
        assert sold.realized_pnl == Decimal("0.428501797797797798")
        assert sold.fees_paid == Decimal("0.05021343")

    def test_selling_everything_resets_cost(self) -> None:
        bought = apply_fill(None, trade_fill(Side.BUY, "0.5", "100", "0", "BTC"), MARKET)
        sold = apply_fill(bought, trade_fill(Side.SELL, "0.5", "110", "0", "USDT"), MARKET)

        assert sold.is_flat
        assert sold.average_cost == 0
        assert sold.realized_pnl == Decimal(5)

    def test_weighted_average_across_buys(self) -> None:
        first = apply_fill(None, trade_fill(Side.BUY, "1", "100", "0", "USDT"), MARKET)
        second = apply_fill(first, trade_fill(Side.BUY, "1", "110", "0.11", "USDT"), MARKET)

        assert second.quantity == Decimal(2)
        assert second.average_cost == Decimal("105.055")  # (100 + 110 + 0.11) / 2

    def test_overselling_is_refused(self) -> None:
        bought = apply_fill(None, trade_fill(Side.BUY, "0.1", "100", "0", "USDT"), MARKET)

        with pytest.raises(AccountingError, match="exceeds the tracked position"):
            apply_fill(bought, trade_fill(Side.SELL, "0.2", "100", "0", "USDT"), MARKET)

    def test_foreign_fee_currency_is_refused(self) -> None:
        with pytest.raises(AccountingError, match="BNB"):
            apply_fill(None, trade_fill(Side.BUY, "0.1", "100", "0.0001", "BNB"), MARKET)

    def test_fill_for_other_market_is_refused(self) -> None:
        with pytest.raises(AccountingError, match="applied to"):
            apply_fill(
                None,
                trade_fill(Side.BUY, "0.1", "100", "0", "USDT"),
                make_market(symbol="ETH/USDT", base="ETH"),
            )

    def test_existing_position_is_extended(self) -> None:
        start = Position("binance", "BTC/USDT", Decimal(1), Decimal(100), Decimal(0), T0)

        after = apply_fill(start, trade_fill(Side.SELL, "0.5", "120", "0", "USDT"), MARKET)

        assert after.realized_pnl == Decimal(10)


class TestFreshness:
    CANDLE = T0  # 12:00 candle closes at 12:05

    def reason(self, minutes_after_close: float, latest: object = CANDLE) -> str | None:
        now = self.CANDLE + FIVE_MIN.duration + timedelta(minutes=minutes_after_close)
        return stale_reason(
            make_signal(source_candle_time=self.CANDLE),
            latest_closed_open=latest,  # type: ignore[arg-type]
            now=now,
            timeframe=FIVE_MIN,
        )

    def test_fresh_within_one_interval(self) -> None:
        assert self.reason(0) is None
        assert self.reason(5) is None

    def test_stale_after_one_interval(self) -> None:
        assert "interval" in str(self.reason(5.01))

    def test_older_candle_is_stale_even_if_recent(self) -> None:
        assert "newer closed candle" in str(self.reason(1, latest=self.CANDLE + FIVE_MIN.duration))

    def test_unknown_latest_candle_is_stale(self) -> None:
        assert self.reason(1, latest=None) is not None

    def test_other_timeframe_is_refused(self) -> None:
        from arbitron.domain.market import Timeframe  # noqa: PLC0415

        signal = make_signal(timeframe=Timeframe("1h"))
        assert stale_reason(signal, latest_closed_open=T0, now=T0, timeframe=FIVE_MIN) is not None
