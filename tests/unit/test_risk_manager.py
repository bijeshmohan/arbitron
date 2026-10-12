"""Risk gate: every limit at its boundary, fail-closed conditions, exits vs entries."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from arbitron.application.policies import risk_policy, risk_policy_version, sizing_policy
from arbitron.config.settings import ArbitronConfig
from arbitron.domain.enums import KillSwitchState, OrderStatus, OrderType, Side
from arbitron.domain.orders import OrderIntent
from arbitron.domain.risk import KillSwitch, RiskReason
from arbitron.domain.snapshot import TradingSnapshot
from arbitron.risk.exposure import (
    ValuationUnavailableError,
    asset_exposure,
    committed_quote,
    valuation,
)
from arbitron.risk.manager import decide, loss_limit_breaches
from arbitron.sizing.sizer import size
from tests.factories import (
    T0,
    balance,
    make_intent,
    make_market,
    make_signal,
    make_snapshot,
    make_ticker,
)
from tests.unit.test_config import example_data
from tests.unit.test_sizing import holding, open_order

R = RiskReason
CONFIG = ArbitronConfig.model_validate(example_data())
POLICY = risk_policy(CONFIG)
SIZING = sizing_policy(CONFIG)


def entry(snapshot: TradingSnapshot | None = None) -> OrderIntent:
    """The intent the sizer produces for a BUY: 0.249 @ 100.11 (notional 24.92739)."""
    result = size(make_signal(side=Side.BUY), snapshot or make_snapshot(), SIZING, now=T0)
    assert result.intent is not None, result
    return result.intent


def exit_all(btc: str = "0.5") -> tuple[OrderIntent, TradingSnapshot]:
    snapshot = make_snapshot(**holding(btc))
    result = size(make_signal(side=Side.SELL), snapshot, SIZING, now=T0)
    assert result.intent is not None, result
    return result.intent, snapshot


def reasons(
    intent: OrderIntent, snapshot: TradingSnapshot | None = None, **policy: Any
) -> list[RiskReason]:
    decision = decide(intent, snapshot or make_snapshot(), replace(POLICY, **policy), now=T0)
    return list(decision.reasons)


def test_sensible_entry_is_approved_with_observations() -> None:
    decision = decide(entry(), make_snapshot(), POLICY, now=T0)

    assert decision.approved, decision.reasons
    assert decision.policy_version == POLICY.version
    assert decision.observations["notional"] == "24.92739"
    assert decision.observations["equity"] == "1000"
    assert decision.observations["quote_after"] == "975.04768261"  # 1000 - 24.92739 * 1.001


def test_sensible_exit_is_approved() -> None:
    intent, snapshot = exit_all()

    assert decide(intent, snapshot, POLICY, now=T0).approved


class TestFailClosedBlocksEverything:
    """These block entries and exits alike (plan §11.4)."""

    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"kill_switch": None}, R.KILL_SWITCH_NOT_ARMED),
            (
                {"kill_switch": KillSwitch(KillSwitchState.TRIPPED, T0, "system", "x")},
                R.KILL_SWITCH_NOT_ARMED,
            ),
            (
                {"kill_switch": KillSwitch(KillSwitchState.RECOVERY_REVIEW, T0, "system", "x")},
                R.KILL_SWITCH_NOT_ARMED,
            ),
            ({"trading_enabled": False}, R.TRADING_DISABLED),
            ({"system_ready": False}, R.SYSTEM_NOT_READY),
            ({"reconciliation_clean": False}, R.RECONCILIATION_UNRESOLVED),
            ({"exchange_healthy": False}, R.EXCHANGE_ERROR_RATE),
            ({"market_data_ok": {"BTC/USDT": False}}, R.MARKET_DATA_STALE),
            ({"market_data_ok": {}}, R.MARKET_DATA_STALE),
            ({"tickers": {}}, R.TICKER_UNAVAILABLE),
        ],
    )
    @pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
    def test_condition(self, overrides: dict[str, Any], reason: RiskReason, side: Side) -> None:
        if side is Side.BUY:
            intent, snapshot = entry(), make_snapshot(**overrides)
        else:
            intent, base = exit_all()
            snapshot = replace(base, **overrides)

        assert reason in reasons(intent, snapshot)

    def test_stale_ticker(self) -> None:
        stale = make_snapshot(as_of=T0 + timedelta(seconds=11))

        assert R.TICKER_UNAVAILABLE in reasons(entry(), stale)

    @pytest.mark.parametrize(
        "snapshot",
        [
            make_snapshot(markets={"BTC/USDT": make_market(active=False)}),
            make_snapshot(markets={}),
        ],
    )
    def test_unknown_or_inactive_market(self, snapshot: TradingSnapshot) -> None:
        assert reasons(entry(), snapshot) == [R.MARKET_NOT_ALLOWED]

    def test_symbol_outside_allowlist(self) -> None:
        assert R.MARKET_NOT_ALLOWED in reasons(entry(), allowed_symbols=frozenset({"ETH/USDT"}))

    def test_market_orders_are_refused(self) -> None:
        market_order = make_intent(order_type=OrderType.MARKET, limit_price=None)

        assert R.MARKET_ORDERS_DISALLOWED in reasons(market_order)

    def test_precision_and_minimums(self) -> None:
        off_lot = replace(entry(), amount=Decimal("0.2495"))
        off_tick = replace(entry(), limit_price=Decimal("100.105"))
        too_small = replace(entry(), amount=Decimal("0.049"))  # 4.9 USDT < min cost 5

        assert R.PRECISION_VIOLATION in reasons(off_lot)
        assert R.PRECISION_VIOLATION in reasons(off_tick)
        assert R.BELOW_MINIMUM in reasons(too_small)

    def test_spread_too_wide(self) -> None:
        wide = make_snapshot(tickers={"BTC/USDT": make_ticker(bid="99.70", ask="100.01")})

        # spread = 0.31 / 99.855 = 0.3104% > 0.2%
        assert R.SPREAD_TOO_WIDE in reasons(entry(), wide)

    def test_limit_outside_slippage_band(self) -> None:
        greedy_buy = replace(entry(), limit_price=Decimal("100.12"))  # cap is 100.11001
        intent, snapshot = exit_all()
        cheap_sell = replace(intent, limit_price=Decimal("99.89"))  # floor is 99.9

        assert R.PRICE_OUTSIDE_BAND in reasons(greedy_buy)
        assert R.PRICE_OUTSIDE_BAND in reasons(cheap_sell, snapshot)
        assert reasons(replace(entry(), limit_price=Decimal("100.11"))) == []


class TestEntryLimits:
    def test_order_notional_boundary(self) -> None:
        assert reasons(entry(), max_order_notional=Decimal("24.92739")) == []
        assert R.MAX_ORDER_NOTIONAL in reasons(entry(), max_order_notional=Decimal("24.92738"))

    def test_asset_exposure_includes_open_buys_at_limit(self) -> None:
        # An open entry on another symbol would not count; here it is a pending
        # BTC entry in a different (unacknowledged) order.
        snapshot = make_snapshot(
            open_orders=(open_order(Side.BUY, "0.5", "100.00", OrderStatus.SUBMITTING),)
        )

        found = reasons(entry(), snapshot, max_asset_exposure=Decimal("74.9"))

        # 0.5 * 100 + 24.92739 = 74.92739 > 74.9
        assert R.MAX_ASSET_EXPOSURE in found

    def test_total_exposure_boundary(self) -> None:
        assert R.MAX_TOTAL_EXPOSURE not in reasons(entry(), max_total_exposure=Decimal("24.92739"))
        assert R.MAX_TOTAL_EXPOSURE in reasons(entry(), max_total_exposure=Decimal("24.92738"))

    def test_enter_only_when_flat(self) -> None:
        assert R.POSITION_POLICY in reasons(entry(), make_snapshot(**holding("0.2")))
        pending = make_snapshot(open_orders=(open_order(Side.BUY, "0.1", "100.00"),))
        assert R.POSITION_POLICY in reasons(entry(), pending)
        assert R.POSITION_POLICY not in reasons(entry(), make_snapshot(**holding("0.0009")))

    def test_open_order_count_boundary(self) -> None:
        one_open = make_snapshot(open_orders=(open_order(Side.SELL, "0.1", "99.90"),))

        assert R.MAX_OPEN_ORDERS not in reasons(entry(), one_open, max_open_orders=2)
        assert R.MAX_OPEN_ORDERS in reasons(entry(), one_open, max_open_orders=1)

    def test_order_frequency_boundary(self) -> None:
        assert reasons(entry(), make_snapshot(orders_last_hour=5)) == []
        assert R.ORDER_FREQUENCY in reasons(entry(), make_snapshot(orders_last_hour=6))

    def test_daily_loss_boundary(self) -> None:
        # Equity 1000; loss reaching the limit stops entries.
        assert R.MAX_DAILY_LOSS not in reasons(
            entry(), make_snapshot(day_start_equity=Decimal("1009.99"))
        )
        assert R.MAX_DAILY_LOSS in reasons(entry(), make_snapshot(day_start_equity=Decimal(1010)))

    def test_drawdown_boundary(self) -> None:
        # 1000 vs peak 1052.63 is a 4.99996% drawdown; vs 1052.64 is 5.00076%.
        assert R.MAX_DRAWDOWN not in reasons(entry(), make_snapshot(peak_equity=Decimal("1052.63")))
        assert R.MAX_DRAWDOWN in reasons(entry(), make_snapshot(peak_equity=Decimal("1052.64")))

    @pytest.mark.parametrize("field", ["day_start_equity", "peak_equity"])
    def test_unknown_loss_baseline_rejects(self, field: str) -> None:
        assert R.VALUATION_UNAVAILABLE in reasons(entry(), make_snapshot(**{field: None}))

    def test_missing_mark_for_held_asset_rejects(self) -> None:
        snapshot = make_snapshot(**holding("0.0009"), marks={})

        assert R.VALUATION_UNAVAILABLE in reasons(entry(), snapshot)

    def test_insufficient_balance(self) -> None:
        poor = make_snapshot(balances={"USDT": balance("USDT", "24.95")})

        # cost with fees = 24.92739 * 1.001 = 24.952317 > 24.95
        assert R.INSUFFICIENT_BALANCE in reasons(entry(), poor)

    def test_reserve_boundary(self) -> None:
        # cost with fees = 24.95231739; reserve 10 -> need 34.95231739 free
        assert R.BALANCE_RESERVE not in reasons(
            entry(), make_snapshot(balances={"USDT": balance("USDT", "34.95231739")})
        )
        assert R.BALANCE_RESERVE in reasons(
            entry(), make_snapshot(balances={"USDT": balance("USDT", "34.95231738")})
        )

    def test_unacknowledged_buys_reduce_available_quote(self) -> None:
        # A SUBMITTING buy elsewhere is not yet in the exchange 'used' balance.
        pending = open_order(Side.BUY, "0.3", "100.00", OrderStatus.SUBMITTING)
        pending = replace(pending, intent=replace(pending.intent, symbol="ETH/USDT"))
        snapshot = make_snapshot(balances={"USDT": balance("USDT", "60")}, open_orders=(pending,))

        # 60 - 30.03 committed = 29.97 available; cost 24.95 leaves 5.02 < reserve 10
        assert R.BALANCE_RESERVE in reasons(entry(), snapshot, max_open_orders=5)

    def test_acknowledged_buys_are_not_double_counted(self) -> None:
        acknowledged = open_order(Side.BUY, "0.3", "100.00", OrderStatus.OPEN)
        acknowledged = replace(acknowledged, intent=replace(acknowledged.intent, symbol="ETH/USDT"))
        snapshot = make_snapshot(
            balances={"USDT": balance("USDT", "60", used="30")}, open_orders=(acknowledged,)
        )

        assert R.BALANCE_RESERVE not in reasons(entry(), snapshot, max_open_orders=5)

    def test_all_violations_are_reported_together(self) -> None:
        snapshot = make_snapshot(
            **holding("0.2"), orders_last_hour=99, trading_enabled=False, kill_switch=None
        )

        found = set(reasons(entry(), snapshot))

        assert {
            R.KILL_SWITCH_NOT_ARMED,
            R.TRADING_DISABLED,
            R.POSITION_POLICY,
            R.ORDER_FREQUENCY,
        } <= found


class TestExitsAreExemptFromRiskIncreasingLimits:
    def test_exit_allowed_despite_loss_exposure_and_frequency_breaches(self) -> None:
        intent, snapshot = exit_all()
        stressed = replace(
            snapshot,
            day_start_equity=Decimal(5000),
            peak_equity=Decimal(9000),
            orders_last_hour=999,
            balances={"BTC": balance("BTC", "0.5")},  # no quote at all
        )

        assert (
            reasons(
                intent,
                stressed,
                max_order_notional=Decimal(1),
                max_open_orders=1,
                max_asset_exposure=Decimal(1),
                max_total_exposure=Decimal(1),
            )
            == []
        )

    def test_cannot_sell_more_than_held(self) -> None:
        intent, snapshot = exit_all("0.5")

        assert R.INSUFFICIENT_POSITION in reasons(replace(intent, amount=Decimal("0.6")), snapshot)

    def test_cannot_sell_quantity_already_committed(self) -> None:
        intent, _ = exit_all("0.5")
        committed = make_snapshot(
            **holding("0.5", free="0.3"), open_orders=(open_order(Side.SELL, "0.2", "99.90"),)
        )

        assert R.INSUFFICIENT_POSITION in reasons(intent, committed)


class TestRobustness:
    def test_intent_does_not_count_against_itself(self) -> None:
        intent = entry()
        from arbitron.domain.orders import Order  # noqa: PLC0415

        self_order = Order.new(intent)
        snapshot = make_snapshot(open_orders=(self_order,))

        assert reasons(intent, snapshot, max_open_orders=1) == []

    def test_internal_failure_rejects(self) -> None:
        broken = replace(POLICY, allowed_symbols=None)  # type: ignore[arg-type]

        decision = decide(entry(), make_snapshot(), broken, now=T0)

        assert decision.reasons == (R.RISK_STATE_UNAVAILABLE,)
        assert "TypeError" in decision.observations["error"]

    def test_decisions_are_deterministic(self) -> None:
        intent = entry()

        assert decide(intent, make_snapshot(), POLICY, now=T0) == decide(
            intent, make_snapshot(), POLICY, now=T0
        )


class TestExposureAndValuation:
    def test_valuation_marks_configured_assets_and_reports_others(self) -> None:
        snapshot = make_snapshot(
            balances={
                "USDT": balance("USDT", "500"),
                "BTC": balance("BTC", "2"),
                "BNB": balance("BNB", "0.3"),
            }
        )

        result = valuation(snapshot)

        assert result.equity == Decimal(700)
        assert result.unvalued_assets == ("BNB",)

    def test_missing_mark_raises(self) -> None:
        with pytest.raises(ValuationUnavailableError, match="no mark"):
            valuation(make_snapshot(**holding("1"), marks={}))

    def test_exposure_counts_held_value_and_open_buys_not_sells(self) -> None:
        snapshot = make_snapshot(
            **holding("1"),
            open_orders=(
                open_order(Side.BUY, "0.5", "101.00"),
                open_order(Side.SELL, "0.4", "99.00"),
            ),
        )

        assert asset_exposure(snapshot, "BTC/USDT") == Decimal("150.5")  # 100 + 50.5

    def test_committed_quote_only_counts_unacknowledged_buys_with_fees(self) -> None:
        snapshot = make_snapshot(
            open_orders=(
                open_order(Side.BUY, "0.1", "100.00", OrderStatus.SUBMITTING),
                open_order(Side.BUY, "0.2", "100.00", OrderStatus.OPEN),
                open_order(Side.SELL, "0.3", "100.00", OrderStatus.SUBMITTING),
            )
        )

        assert committed_quote(snapshot, Decimal("0.001")) == Decimal("10.01")

    def test_loss_breaches_for_kill_switch_decisions(self) -> None:
        stressed = make_snapshot(day_start_equity=Decimal(1100), peak_equity=Decimal(1100))

        assert loss_limit_breaches(stressed, POLICY, Decimal(1000)) == (
            R.MAX_DAILY_LOSS,
            R.MAX_DRAWDOWN,
        )
        assert loss_limit_breaches(make_snapshot(), POLICY, Decimal(1000)) == ()


class TestPolicies:
    def test_policy_values_come_from_config(self) -> None:
        assert POLICY.fee_rate == Decimal("0.001")
        assert POLICY.max_ticker_age == timedelta(seconds=10)
        assert POLICY.allowed_symbols == frozenset({"BTC/USDT"})
        assert POLICY.version.startswith("risk-")

    def test_version_changes_with_any_limit(self) -> None:
        data = example_data()
        data["risk"]["max_daily_loss"] = "11"

        assert risk_policy_version(ArbitronConfig.model_validate(data)) != POLICY.version
        assert risk_policy_version(CONFIG) == POLICY.version


def test_snapshot_rejects_terminal_open_orders() -> None:
    from arbitron.domain.errors import DomainValidationError  # noqa: PLC0415

    filled = replace(open_order(Side.BUY, "0.1", "100.00"), status=OrderStatus.CANCELED)
    with pytest.raises(DomainValidationError, match="terminal"):
        make_snapshot(open_orders=(filled,))


def test_snapshot_rejects_market_with_other_quote() -> None:
    from arbitron.domain.errors import DomainValidationError  # noqa: PLC0415

    eth_btc = make_market(symbol="ETH/BTC", base="ETH", quote="BTC")
    with pytest.raises(DomainValidationError, match="quote"):
        make_snapshot(markets={"ETH/BTC": eth_btc})
