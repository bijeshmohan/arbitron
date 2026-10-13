"""Backtest engine and metrics against hand-calculated toy datasets."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from arbitron.application.policies import risk_policy, sizing_policy
from arbitron.backtest import engine, metrics
from arbitron.backtest.engine import Assumptions, Inputs
from arbitron.config.settings import ArbitronConfig
from arbitron.domain.enums import Side
from arbitron.domain.market import Candle
from arbitron.domain.strategies import ParameterValue
from arbitron.strategy.base import SignalProposal, StrategyContext
from arbitron.strategy.implementations.sma_cross import SmaCross
from tests.factories import FIVE_MIN, T0, make_candle, make_market
from tests.unit.test_config import example_data

CONFIG = ArbitronConfig.model_validate(example_data())
MARKET = make_market()  # lot 0.001, tick 0.01, min 0.001 / 5 USDT
ASSUME = Assumptions(
    starting_balance=Decimal(1000),
    spread_bps=Decimal(2),
    slippage_bps=Decimal(5),
    fee_rate=Decimal("0.001"),
)


def series(*prices: str | tuple[str, str], start: int = 0) -> list[Candle]:
    """Candles from closes (open == close), or (open, close) pairs."""
    out: list[Candle] = []
    for i, price in enumerate(prices, start=start):
        open_, close = (price, price) if isinstance(price, str) else price
        o, c = Decimal(open_), Decimal(close)
        out.append(
            make_candle(
                open_time=T0 + i * FIVE_MIN.duration,
                ingested_at=T0 + (i + 1) * FIVE_MIN.duration,
                open=o,
                close=c,
                high=max(o, c),
                low=min(o, c),
            )
        )
    return out


class Scripted:
    strategy_id = "scripted"
    version = "1"
    lookback = 2
    parameters: Mapping[str, ParameterValue] = {}

    def __init__(self, script: Mapping[int, Side]) -> None:
        self.script = {T0 + i * FIVE_MIN.duration: s for i, s in script.items()}

    def evaluate(self, context: StrategyContext) -> Sequence[SignalProposal]:
        side = self.script.get(context.current.open_time)
        return [SignalProposal(side, "scripted")] if side else []


def run(
    candles: list[Candle],
    script: Mapping[int, Side],
    *,
    start: int = 1,
    assumptions: Assumptions = ASSUME,
    config: ArbitronConfig = CONFIG,
) -> engine.Result:
    return engine.run(
        Inputs(
            strategy=Scripted(script),  # type: ignore[arg-type]
            sizing=sizing_policy(config),
            risk=risk_policy(config),
            markets={"BTC/USDT": MARKET},
            candles={"BTC/USDT": candles},
            timeframe=FIVE_MIN,
            quote_currency="USDT",
            trading_enabled=config.risk.trading_enabled,
            start=T0 + start * FIVE_MIN.duration,
        ),
        assumptions,
    )


class TestHandCalculatedRoundTrip:
    """Flat price 100; BUY on candle 2, SELL on candle 5 (see module docstring math)."""

    def result(self) -> engine.Result:
        return run(series(*["100"] * 10), {2: Side.BUY, 5: Side.SELL})

    def test_fills_at_next_open_with_spread_slippage_and_fees(self) -> None:
        buy, sell = self.result().trades

        # BUY decided at close of candle 2 fills at the open of candle 3.
        assert buy.time == T0 + 3 * FIVE_MIN.duration
        # quote at 100: ask 100.01; +5 bps = 100.060005 -> tick up -> 100.07
        assert (buy.amount, buy.price) == (Decimal("0.249"), Decimal("100.07"))
        assert (buy.fee, buy.fee_currency) == (Decimal("0.000249"), "BTC")
        # SELL decided at close of candle 5 fills at the open of candle 6:
        # bid 99.99; -5 bps = 99.940005 -> tick down -> 99.94; lot 0.248 (dust 0.000751)
        assert sell.time == T0 + 6 * FIVE_MIN.duration
        assert (sell.amount, sell.price) == (Decimal("0.248"), Decimal("99.94"))
        assert (sell.fee, sell.fee_currency) == (Decimal("0.02478512"), "USDT")

    def test_balances_and_equity(self) -> None:
        result = self.result()

        # 1000 - 0.249 x 100.07 + 0.248 x 99.94 - 0.02478512
        assert result.final_balances["USDT"] == Decimal("999.84290488")
        assert result.final_balances["BTC"] == Decimal("0.000751")
        # plus 0.000751 BTC dust marked at 100
        assert result.equity[-1].equity == Decimal("999.91800488")
        assert result.final_positions["BTC/USDT"].quantity == Decimal("0.000751")

    def test_metrics(self) -> None:
        result = self.result()

        m = metrics.compute(
            result, ASSUME, timeframe=FIVE_MIN, candles={"BTC/USDT": series(*["100"] * 10)}
        )

        assert m["total_return_pct"] == "-0.0082"  # (999.918... / 1000 - 1) x 100
        assert m["trades"] == 2
        assert m["round_trips"] == 1
        assert m["win_rate_pct"] == "0"
        # fees: 0.000249 BTC at 100.07 (0.02491743) + 0.02478512 USDT
        assert m["fees_paid"] == "0.0497"
        assert m["benchmark_buy_hold_pct"] == "0"
        assert m["signals"] == 2
        assert m["orders_approved"] == 2
        assert m["halted_at"] is None
        # In the market from the close of candle 3 (bought at its open) to candle 5;
        # the 0.000751 BTC left after selling is dust and does not count.
        assert m["exposure_pct"] == "33.3333"  # 3 of 9 closes (candles 3, 4, 5)


class TestModeling:
    def test_no_lookahead_decision_uses_only_the_closed_candle(self) -> None:
        # The candle after the signal gaps far above the IOC cap: the buy expires.
        candles = series("100", "100", "100", ("150", "150"), "150")
        result = run(candles, {2: Side.BUY})

        assert result.trades == []
        assert result.expired == 1

    def test_gap_in_data_expires_pending_order(self) -> None:
        candles = series(*["100"] * 6)
        del candles[3]  # the candle the buy would fill on is missing

        result = run(candles, {2: Side.BUY})

        assert result.trades == []
        assert result.expired == 1

    def test_signal_on_last_candle_cannot_fill(self) -> None:
        result = run(series(*["100"] * 4), {3: Side.BUY})

        assert result.trades == []
        assert result.expired == 1

    def test_warm_up_candles_produce_no_decisions(self) -> None:
        result = run(series(*["100"] * 8), {1: Side.BUY, 5: Side.BUY}, start=3)

        assert result.signals == 1  # only candle 5 is on or after start
        assert result.equity[0].time == T0 + 4 * FIVE_MIN.duration  # close of candle 3

    def test_risk_gate_applies_flat_only_rule(self) -> None:
        result = run(series(*["100"] * 10), {2: Side.BUY, 4: Side.BUY})

        assert len(result.trades) == 1
        assert result.not_sized == {"already_in_position": 1}

    def test_trading_disabled_rejects_everything(self) -> None:
        data = example_data()
        data["risk"]["trading_enabled"] = False
        disabled = ArbitronConfig.model_validate(data)

        result = run(series(*["100"] * 6), {2: Side.BUY}, config=disabled)

        assert result.trades == []
        assert result.rejected == {"trading_disabled": 1}

    def test_loss_limit_trips_kill_switch_and_halts(self) -> None:
        # Buy at 100, then the price collapses: the 10 USDT daily loss limit breaches.
        crash = series("100", "100", "100", "100", ("40", "40"), "40", "40", "40", "40")
        data = example_data()
        data["risk"]["max_daily_loss"] = "5"
        tight = ArbitronConfig.model_validate(data)

        result = run(crash, {2: Side.BUY, 6: Side.SELL}, config=tight)

        assert result.halted_at is not None
        assert "max_daily_loss" in str(result.halt_reason)
        assert result.rejected.get("kill_switch_not_armed") == 1  # the later SELL
        assert len(result.trades) == 1  # still holding: no operator to re-arm

    def test_deterministic(self) -> None:
        candles = series(*[str(100 + (i % 7) * 3) for i in range(80)])
        strategy_inputs = Inputs(
            strategy=SmaCross(fast=3, slow=8),
            sizing=sizing_policy(CONFIG),
            risk=risk_policy(CONFIG),
            markets={"BTC/USDT": MARKET},
            candles={"BTC/USDT": candles},
            timeframe=FIVE_MIN,
            quote_currency="USDT",
            trading_enabled=True,
            start=T0 + 10 * FIVE_MIN.duration,
        )

        first = engine.run(strategy_inputs, ASSUME)
        second = engine.run(strategy_inputs, ASSUME)

        assert first == second
        assert first.signals > 0

    def test_costs_scale_with_assumptions(self) -> None:
        free = replace(ASSUME, spread_bps=Decimal(0), slippage_bps=Decimal(0), fee_rate=Decimal(0))
        costly = run(series(*["100"] * 10), {2: Side.BUY, 5: Side.SELL})
        cheap = run(series(*["100"] * 10), {2: Side.BUY, 5: Side.SELL}, assumptions=free)

        assert cheap.equity[-1].equity > costly.equity[-1].equity


class TestMetricsFormulas:
    def test_drawdown_and_duration(self) -> None:
        result = engine.Result(
            equity=[
                engine.EquityPoint(T0 + timedelta(minutes=5 * i), Decimal(e), Decimal(0))
                for i, e in enumerate(["100", "120", "90", "96", "130", "117"])
            ]
        )

        m = metrics.compute(
            result, replace(ASSUME, starting_balance=Decimal(100)), timeframe=FIVE_MIN, candles={}
        )

        assert m["max_drawdown_pct"] == "25"  # 120 -> 90
        assert m["max_drawdown_duration"] == str(timedelta(minutes=10))  # 2 candles below peak
        assert m["total_return_pct"] == "17"

    def test_volatility_of_constant_equity_is_zero(self) -> None:
        result = engine.Result(
            equity=[
                engine.EquityPoint(T0 + timedelta(minutes=5 * i), Decimal(1000), Decimal(0))
                for i in range(5)
            ]
        )

        m = metrics.compute(result, ASSUME, timeframe=FIVE_MIN, candles={})

        assert m["volatility_annualized_pct"] == "0"
        assert m["sharpe_ratio_rf0"] is None

    def test_benchmark_net_of_two_fees(self) -> None:
        candles = series(("100", "100"), ("100", "110"))
        result = engine.Result(
            equity=[engine.EquityPoint(c.close_time, Decimal(1000), Decimal(0)) for c in candles]
        )

        m = metrics.compute(result, ASSUME, timeframe=FIVE_MIN, candles={"BTC/USDT": candles})

        assert m["benchmark_buy_hold_pct"] == "10"
        # 1.1 x 0.999 x 0.999 - 1 = 0.0978011
        assert m["benchmark_buy_hold_net_pct"] == "9.7801"


@pytest.mark.parametrize("spread", ["0", "2", "50"])
def test_synthetic_quote_brackets_price(spread: str) -> None:
    from arbitron.simulation.quotes import synthetic_quote  # noqa: PLC0415

    quote = synthetic_quote(
        price=Decimal(100), spread_bps=Decimal(spread), market=MARKET, timestamp=T0
    )

    assert quote.bid is not None
    assert quote.ask is not None
    assert quote.bid <= Decimal(100) <= quote.ask
    assert quote.ask > quote.bid
