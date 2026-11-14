"""Research strategies: buy_hold baseline, trend_filter, breakout, mean_reversion."""

from collections.abc import Sequence
from decimal import Decimal

import pytest

from arbitron.domain.enums import Side
from arbitron.domain.market import Candle
from arbitron.domain.portfolio import PortfolioView
from arbitron.strategy.base import SignalProposal, Strategy, StrategyContext
from arbitron.strategy.implementations._support import StrategyParameterError, rsi
from arbitron.strategy.implementations.breakout import Breakout
from arbitron.strategy.implementations.buy_hold import BuyHold
from arbitron.strategy.implementations.mean_reversion import MeanReversion
from arbitron.strategy.implementations.trend_filter import TrendFilter
from arbitron.strategy.registry import REGISTRY, StrategyConfigError, build_strategy
from tests.factories import FIVE_MIN, T0, make_candle


def bars(*bounds: tuple[int, int, int]) -> list[Candle]:
    """Contiguous candles from (high, low, close) triples."""
    return [
        make_candle(
            open_time=T0 + i * FIVE_MIN.duration,
            ingested_at=T0 + (i + 1) * FIVE_MIN.duration,
            open=Decimal(close),
            high=Decimal(max(high, close)),
            low=Decimal(min(low, close)),
            close=Decimal(close),
        )
        for i, (high, low, close) in enumerate(bounds)
    ]


def closes(*values: int) -> list[Candle]:
    return bars(*((v, v, v) for v in values))


def run(strategy: Strategy, candles: Sequence[Candle]) -> list[SignalProposal]:
    assert len(candles) == strategy.lookback
    context = StrategyContext(
        "binance", "BTC/USDT", FIVE_MIN, tuple(candles), PortfolioView(as_of=T0)
    )
    return list(strategy.evaluate(context))


class TestBuyHold:
    def test_always_proposes_a_buy_and_never_a_sell(self) -> None:
        (proposal,) = run(BuyHold(), closes(100))
        assert proposal.side is Side.BUY

    def test_takes_no_parameters(self) -> None:
        with pytest.raises(StrategyParameterError):
            BuyHold.from_parameters({"x": 1})


class TestTrendFilter:
    def test_buys_above_average_and_sells_below(self) -> None:
        strategy = TrendFilter(length=3)
        (up,) = run(strategy, closes(10, 10, 16))  # average 12, close 16
        (down,) = run(strategy, closes(10, 10, 4))  # average 8, close 4
        assert (up.side, up.reason_code) == (Side.BUY, "close_above_sma")
        assert (down.side, down.reason_code) == (Side.SELL, "close_below_sma")

    def test_close_equal_to_average_is_no_signal(self) -> None:
        assert run(TrendFilter(length=3), closes(9, 15, 12)) == []

    def test_lookback_is_the_length(self) -> None:
        assert TrendFilter(length=50).lookback == 50

    def test_rejects_bad_parameters(self) -> None:
        with pytest.raises(StrategyParameterError):
            TrendFilter(length=0)
        with pytest.raises(StrategyParameterError):
            TrendFilter.from_parameters({"length": True})
        with pytest.raises(StrategyParameterError):
            TrendFilter.from_parameters({"length": 5, "extra": 1})
        with pytest.raises(StrategyParameterError):
            TrendFilter.from_parameters({})


class TestBreakout:
    def test_buys_a_close_above_the_prior_high(self) -> None:
        strategy = Breakout(entry=3, exit=2)
        candles = bars((10, 8, 9), (11, 9, 10), (12, 10, 11), (14, 11, 13))
        (proposal,) = run(strategy, candles)
        assert (proposal.side, proposal.reason_code) == (Side.BUY, "close_above_channel_high")

    def test_the_current_candle_is_not_part_of_its_own_channel(self) -> None:
        # The current high (20) must not raise the bar its own close is tested against.
        strategy = Breakout(entry=3, exit=2)
        candles = bars((10, 8, 9), (11, 9, 10), (12, 10, 11), (20, 11, 12))
        assert run(strategy, candles) == []

    def test_sells_a_close_below_the_prior_low(self) -> None:
        strategy = Breakout(entry=3, exit=2)
        candles = bars((12, 10, 11), (12, 10, 11), (12, 10, 11), (11, 7, 8))
        (proposal,) = run(strategy, candles)
        assert (proposal.side, proposal.reason_code) == (Side.SELL, "close_below_channel_low")

    def test_inside_the_channel_is_no_signal(self) -> None:
        strategy = Breakout(entry=3, exit=2)
        candles = bars((12, 10, 11), (12, 10, 11), (12, 10, 11), (12, 10, 11))
        assert run(strategy, candles) == []

    def test_exit_window_is_the_shorter_one(self) -> None:
        strategy = Breakout(entry=4, exit=2)
        # The 4-candle low is 5, the 2-candle low is 9; a close of 8 sells only via exit.
        candles = bars((12, 5, 6), (12, 10, 11), (12, 9, 11), (12, 9, 11), (11, 8, 8))
        (proposal,) = run(strategy, candles)
        assert proposal.side is Side.SELL

    def test_lookback_covers_the_longer_window_plus_the_current_candle(self) -> None:
        assert Breakout(entry=20, exit=10).lookback == 21
        assert Breakout(entry=10, exit=20).lookback == 21

    def test_rejects_bad_parameters(self) -> None:
        with pytest.raises(StrategyParameterError):
            Breakout(entry=0, exit=5)
        with pytest.raises(StrategyParameterError):
            Breakout.from_parameters({"entry": 5})


class TestRsi:
    def test_all_gains_is_100_and_all_losses_is_0(self) -> None:
        up = [Decimal(v) for v in range(1, 12)]
        assert rsi(up, 5) == 100
        assert rsi(list(reversed(up)), 5) == 0

    def test_flat_prices_are_neutral(self) -> None:
        assert rsi([Decimal(5)] * 12, 5) == 50

    def test_matches_a_hand_computed_value(self) -> None:
        # length 2: changes +2, -1, +3. Seed avg gain 1, avg loss 0.5;
        # then smoothed with +3: gain (1*1+3)/2 = 2, loss (0.5*1+0)/2 = 0.25.
        values = [Decimal(v) for v in (10, 12, 11, 14)]
        assert rsi(values, 2) == pytest.approx(Decimal(800) / 9)


class TestMeanReversion:
    def test_buys_after_a_sharp_fall_and_sells_after_a_rise(self) -> None:
        strategy = MeanReversion(length=3, oversold=30, exit_level=70)
        (buy,) = run(strategy, closes(98, 95, 91, 86, 80, 73, 65))
        (sell,) = run(strategy, closes(65, 73, 80, 86, 91, 95, 98))
        assert (buy.side, buy.reason_code) == (Side.BUY, "rsi_oversold")
        assert (sell.side, sell.reason_code) == (Side.SELL, "rsi_recovered")

    def test_middle_range_is_no_signal(self) -> None:
        strategy = MeanReversion(length=3, oversold=30, exit_level=70)
        assert run(strategy, closes(11, 10, 11, 10, 11, 10, 11)) == []

    def test_lookback_is_fixed_by_length(self) -> None:
        assert MeanReversion(length=14, oversold=30, exit_level=55).lookback == 29

    @pytest.mark.parametrize(("oversold", "exit_level"), [(0, 50), (50, 50), (60, 40), (30, 100)])
    def test_rejects_bad_thresholds(self, oversold: int, exit_level: int) -> None:
        with pytest.raises(StrategyParameterError):
            MeanReversion(length=14, oversold=oversold, exit_level=exit_level)


class TestRegistration:
    @pytest.mark.parametrize(
        ("strategy_id", "parameters"),
        [
            ("buy_hold", {}),
            ("trend_filter", {"length": 5}),
            ("breakout", {"entry": 5, "exit": 3}),
            ("mean_reversion", {"length": 5, "oversold": 30, "exit_level": 55}),
        ],
    )
    def test_research_strategies_build_and_are_refused_in_live(
        self, strategy_id: str, parameters: dict[str, int]
    ) -> None:
        assert not REGISTRY[strategy_id].allowed_in_live
        strategy = build_strategy(strategy_id, "1", parameters)
        assert strategy.strategy_id == strategy_id
        assert dict(strategy.parameters) == parameters
        with pytest.raises(StrategyConfigError, match="not allowed in live"):
            build_strategy(strategy_id, "1", parameters, live=True)

    def test_bad_parameters_surface_as_config_errors(self) -> None:
        with pytest.raises(StrategyConfigError):
            build_strategy("trend_filter", "1", {"length": 0})
