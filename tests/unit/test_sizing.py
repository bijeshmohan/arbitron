"""Sizer: fixed quote notional, enter flat / exit all, IOC marketable limits."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from arbitron.domain.enums import OrderStatus, OrderType, Side, TimeInForce
from arbitron.domain.market import Ticker
from arbitron.domain.orders import Order
from arbitron.domain.signals import Signal
from arbitron.sizing.sizer import POLICY_NAME, NoIntentReason, SizingPolicy, size
from tests.factories import (
    T0,
    balance,
    make_intent,
    make_market,
    make_signal,
    make_snapshot,
    make_ticker,
)

N = NoIntentReason
POLICY = SizingPolicy(
    entry_notional=Decimal(25),
    max_slippage_pct=Decimal("0.1"),
    max_ticker_age=timedelta(seconds=10),
)
BUY = make_signal(side=Side.BUY)
SELL = make_signal(side=Side.SELL)


def open_order(
    side: Side, amount: str, limit: str, status: OrderStatus = OrderStatus.OPEN
) -> Order:
    intent = make_intent(side=side, amount=Decimal(amount), limit_price=Decimal(limit), attempt=7)
    return replace(
        Order.new(intent),
        status=status,
        exchange_order_id="ex-9" if status is OrderStatus.OPEN else None,
    )


def holding(btc: str, *, free: str | None = None) -> dict[str, object]:
    used = str(Decimal(btc) - Decimal(free)) if free is not None else "0"
    return {
        "balances": {
            "USDT": balance("USDT", "1000"),
            "BTC": balance("BTC", free if free is not None else btc, used),
        }
    }


class TestEntry:
    def test_sizes_fixed_notional_at_capped_ask(self) -> None:
        result = size(BUY, make_snapshot(), POLICY, now=T0)

        intent = result.intent
        assert result.reason is N.SIZED
        assert intent is not None
        # cap = 100.01 * 1.001 = 100.11001 -> tick 0.01 down -> 100.11
        assert intent.limit_price == Decimal("100.11")
        # 25 / 100.11 = 0.24972... -> lot 0.001 down -> 0.249
        assert intent.amount == Decimal("0.249")
        assert intent.limit_price is not None
        assert intent.amount * intent.limit_price <= POLICY.entry_notional
        assert (intent.side, intent.order_type, intent.time_in_force) == (
            Side.BUY,
            OrderType.LIMIT,
            TimeInForce.IOC,
        )
        assert intent.sizing_policy == POLICY_NAME
        assert intent.sizing_inputs["ask"] == "100.01"
        assert intent.signal_id == BUY.id

    def test_already_holding_a_position(self) -> None:
        result = size(BUY, make_snapshot(**holding("0.2")), POLICY, now=T0)

        assert (result.intent, result.reason) == (None, N.ALREADY_IN_POSITION)

    def test_dust_counts_as_flat(self) -> None:
        # 0.0009 BTC is below the 0.001 lot minimum: it cannot be sold.
        result = size(BUY, make_snapshot(**holding("0.0009")), POLICY, now=T0)

        assert result.reason is N.SIZED

    def test_open_entry_blocks_another(self) -> None:
        snapshot = make_snapshot(open_orders=(open_order(Side.BUY, "0.2", "100.10"),))

        assert size(BUY, snapshot, POLICY, now=T0).reason is N.ENTRY_PENDING

    def test_below_venue_minimum(self) -> None:
        tiny = replace(POLICY, entry_notional=Decimal(4))

        assert size(BUY, make_snapshot(), tiny, now=T0).reason is N.BELOW_MINIMUM

    def test_tiny_slippage_still_allows_the_ask_itself(self) -> None:
        # cap = 100.01 * 1.00001 = 100.011 -> rounds down to the ask: a valid order at the ask.
        narrow = replace(POLICY, max_slippage_pct=Decimal("0.001"))

        intent = size(BUY, make_snapshot(), narrow, now=T0).intent

        assert intent is not None
        assert intent.limit_price == Decimal("100.01")

    def test_off_tick_quote_with_band_narrower_than_tick(self) -> None:
        # Defensive: a venue quoting off-tick (100.015) with a tiny band cannot be priced.
        narrow = replace(POLICY, max_slippage_pct=Decimal("0.001"))
        snapshot = make_snapshot(tickers={"BTC/USDT": make_ticker(bid="100.005", ask="100.015")})

        assert size(BUY, snapshot, narrow, now=T0).reason is N.BAND_NARROWER_THAN_TICK
        assert (
            size(SELL, replace(snapshot, **holding("0.5")), narrow, now=T0).reason
            is N.BAND_NARROWER_THAN_TICK
        )

    def test_real_binance_btc_usdt_steps(self) -> None:
        market = make_market(
            amount_step=Decimal("0.00001"),
            price_step=Decimal("0.01"),
            min_amount=Decimal("0.00001"),
        )
        snapshot = make_snapshot(
            markets={"BTC/USDT": market},
            tickers={"BTC/USDT": make_ticker(bid="83157.91", ask="83157.92")},
        )

        intent = size(BUY, snapshot, POLICY, now=T0).intent

        assert intent is not None
        # cap = 83157.92 * 1.001 = 83241.07792 -> 83241.07; 25 / 83241.07 -> 0.00030
        assert intent.limit_price == Decimal("83241.07")
        assert intent.amount == Decimal("0.00030")


class TestExit:
    def test_sells_whole_position_at_floored_bid(self) -> None:
        intent = size(SELL, make_snapshot(**holding("0.5")), POLICY, now=T0).intent

        assert intent is not None
        assert intent.side is Side.SELL
        assert intent.amount == Decimal("0.5")
        # floor = 100.00 * 0.999 = 99.9 -> tick 0.01 up -> 99.90
        assert intent.limit_price == Decimal("99.90")
        assert intent.time_in_force is TimeInForce.IOC

    def test_floor_rounds_up_never_below(self) -> None:
        snapshot = make_snapshot(
            **holding("0.5"), tickers={"BTC/USDT": make_ticker(bid="100.07", ask="100.08")}
        )

        intent = size(SELL, snapshot, POLICY, now=T0).intent

        # floor = 100.07 * 0.999 = 99.96993 -> up to 99.97 (never below the floor)
        assert intent is not None
        assert intent.limit_price == Decimal("99.97")

    def test_lot_rounding_leaves_dust(self) -> None:
        intent = size(SELL, make_snapshot(**holding("0.5004")), POLICY, now=T0).intent

        assert intent is not None
        assert intent.amount == Decimal("0.500")

    def test_open_sells_are_subtracted(self) -> None:
        snapshot = make_snapshot(
            **holding("0.5", free="0.3"), open_orders=(open_order(Side.SELL, "0.2", "99.90"),)
        )

        intent = size(SELL, snapshot, POLICY, now=T0).intent

        assert intent is not None
        assert intent.amount == Decimal("0.3")

    def test_position_fully_committed_to_open_sell(self) -> None:
        snapshot = make_snapshot(
            **holding("0.5", free="0"), open_orders=(open_order(Side.SELL, "0.5", "99.90"),)
        )

        assert size(SELL, snapshot, POLICY, now=T0).reason is N.EXIT_PENDING

    @pytest.mark.parametrize("held", ["0", "0.0009"])
    def test_nothing_tradeable_to_sell(self, held: str) -> None:
        result = size(SELL, make_snapshot(**holding(held)), POLICY, now=T0)

        assert (result.intent, result.reason) == (None, N.NO_POSITION)


class TestPreconditions:
    @pytest.mark.parametrize("signal", [BUY, SELL])
    def test_unknown_or_inactive_market(self, signal: Signal) -> None:
        inactive = make_snapshot(markets={"BTC/USDT": make_market(active=False)})

        assert size(signal, inactive, POLICY, now=T0).reason is N.MARKET_UNAVAILABLE
        assert (
            size(signal, make_snapshot(markets={}), POLICY, now=T0).reason is N.MARKET_UNAVAILABLE
        )

    @pytest.mark.parametrize(
        "tickers",
        [{}, {"BTC/USDT": Ticker("binance", "BTC/USDT", None, Decimal("100.01"), None, T0)}],
    )
    def test_missing_ticker_or_side(self, tickers: dict[str, Ticker]) -> None:
        snapshot = make_snapshot(tickers=tickers)

        assert size(BUY, snapshot, POLICY, now=T0).reason is N.TICKER_UNAVAILABLE

    def test_stale_ticker(self) -> None:
        result = size(BUY, make_snapshot(), POLICY, now=T0 + timedelta(seconds=11))

        assert result.reason is N.TICKER_UNAVAILABLE

    def test_new_attempt_changes_intent_identity(self) -> None:
        first = size(BUY, make_snapshot(), POLICY, now=T0).intent
        retry = size(BUY, make_snapshot(), POLICY, now=T0, attempt=2).intent

        assert first is not None
        assert retry is not None
        assert first.client_order_id != retry.client_order_id
