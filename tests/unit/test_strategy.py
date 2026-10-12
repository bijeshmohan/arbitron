"""Strategy interface, SMA crossover, shared evaluation, replay, and registry."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal, localcontext

import pytest

from arbitron.domain.enums import Side
from arbitron.domain.errors import DomainValidationError
from arbitron.domain.market import Candle, Timeframe
from arbitron.domain.money import FINANCIAL_CONTEXT
from arbitron.domain.portfolio import PortfolioView
from arbitron.domain.strategies import ParameterValue, StrategyConfig
from arbitron.strategy.base import SignalProposal, StrategyContext, StrategyError
from arbitron.strategy.evaluation import config_of, evaluate_candle, replay, window_problem
from arbitron.strategy.implementations.sma_cross import SmaCross, SmaCrossParameterError
from arbitron.strategy.registry import (
    StrategyConfigError,
    UnknownStrategyError,
    build_strategy,
)
from tests.factories import FIVE_MIN, T0, make_candle

EMPTY = PortfolioView(as_of=T0)


def closes(*values: int | str) -> list[Candle]:
    """Contiguous closed 5m candles with the given closes (OHLC kept consistent)."""
    candles: list[Candle] = []
    for i, value in enumerate(values):
        close = Decimal(value)
        candles.append(
            make_candle(
                open_time=T0 + i * FIVE_MIN.duration,
                ingested_at=T0 + (i + 1) * FIVE_MIN.duration,
                open=close,
                high=close,
                low=close,
                close=close,
            )
        )
    return candles


def evaluate(strategy: SmaCross, candles: Sequence[Candle]) -> list[SignalProposal]:
    context = StrategyContext("binance", "BTC/USDT", FIVE_MIN, tuple(candles), EMPTY)
    return list(strategy.evaluate(context))


class TestStrategyConfig:
    def test_hash_is_stable_and_order_independent(self) -> None:
        a = StrategyConfig("sma_cross", "1", {"fast": 2, "slow": 3})
        b = StrategyConfig("sma_cross", "1", {"slow": 3, "fast": 2})

        assert a.config_hash == b.config_hash
        assert len(a.config_hash) == 64

    @pytest.mark.parametrize(
        "other",
        [
            StrategyConfig("sma_cross", "2", {"fast": 2, "slow": 3}),
            StrategyConfig("sma_cross", "1", {"fast": 2, "slow": 4}),
            StrategyConfig("other", "1", {"fast": 2, "slow": 3}),
            StrategyConfig("sma_cross", "1", {"fast": "2", "slow": 3}),  # type matters
        ],
    )
    def test_hash_changes_with_any_field(self, other: StrategyConfig) -> None:
        assert (
            other.config_hash
            != StrategyConfig("sma_cross", "1", {"fast": 2, "slow": 3}).config_hash
        )

    @pytest.mark.parametrize("value", [1.5, None, [1], {"a": 1}])
    def test_rejects_non_scalar_or_float_parameters(self, value: object) -> None:
        with pytest.raises(DomainValidationError, match="must be str, int, or bool"):
            StrategyConfig("x", "1", {"p": value})  # type: ignore[dict-item]

    def test_parameters_are_read_only(self) -> None:
        config = StrategyConfig("x", "1", {"p": 1})

        with pytest.raises(TypeError):
            config.parameters["p"] = 2  # type: ignore[index]


class TestSmaCross:
    def test_lookback_and_parameters(self) -> None:
        strategy = SmaCross(fast=2, slow=3)

        assert strategy.lookback == 4
        assert strategy.parameters == {"fast": 2, "slow": 3}

    @pytest.mark.parametrize(("fast", "slow"), [(0, 3), (3, 3), (4, 3), (-1, 3), (True, 3)])
    def test_invalid_periods(self, fast: int, slow: int) -> None:
        with pytest.raises(SmaCrossParameterError):
            SmaCross(fast=fast, slow=slow)

    @pytest.mark.parametrize(
        "parameters",
        [
            {},
            {"fast": 2},
            {"fast": 2, "slow": 3, "extra": 1},
            {"fast": "2", "slow": 3},
            {"fast": True, "slow": 3},
        ],
    )
    def test_invalid_parameter_maps(self, parameters: Mapping[str, ParameterValue]) -> None:
        with pytest.raises(SmaCrossParameterError):
            SmaCross.from_parameters(parameters)

    # Hand-calculated with fast=2, slow=3 over four closes [c0, c1, c2, c3]:
    #   previous: fast=(c1+c2)/2, slow=(c0+c1+c2)/3
    #   current:  fast=(c2+c3)/2, slow=(c1+c2+c3)/3
    def test_cross_above_emits_buy(self) -> None:
        # prev fast 10 <= prev slow 10; now fast 12.5 > slow 11.6667
        [proposal] = evaluate(SmaCross(fast=2, slow=3), closes(10, 10, 10, 15))

        assert proposal.side is Side.BUY
        assert proposal.reason_code == "sma_fast_crossed_above_slow"
        assert proposal.metadata["fast_sma"] == "12.5"
        with localcontext(FINANCIAL_CONTEXT):
            assert Decimal(proposal.metadata["slow_sma"]) == Decimal(35) / 3

    def test_rising_then_dipping_crosses_below(self) -> None:
        # prev [10,12,12]: fast 12 >= slow 11.33; now [12,12,11]: fast 11.5 < slow 11.67
        [proposal] = evaluate(SmaCross(fast=2, slow=3), closes(10, 12, 12, 11))

        assert proposal.side is Side.SELL

    def test_cross_below_emits_sell(self) -> None:
        # prev fast 10 >= prev slow 10; now fast 7.5 < slow 8.333...
        [proposal] = evaluate(SmaCross(fast=2, slow=3), closes(10, 10, 10, 5))

        assert proposal.side is Side.SELL
        assert proposal.reason_code == "sma_fast_crossed_below_slow"

    @pytest.mark.parametrize(
        "series",
        [
            (10, 10, 10, 10),  # equal throughout: no cross
            (10, 12, 14, 16),  # fast already above slow and stays above
            (16, 14, 12, 10),  # fast already below slow and stays below
            (10, 12, 14, 13),  # prev fast 13 > slow 12; now fast 13.5 > slow 13: stays above
        ],
    )
    def test_no_cross_no_signal(self, series: tuple[int, ...]) -> None:
        assert evaluate(SmaCross(fast=2, slow=3), closes(*series)) == []

    def test_touching_then_equal_is_not_a_cross(self) -> None:
        # prev fast < prev slow, now exactly equal: must not emit.
        # c = [12, 9, 9, x]: prev fast 9 < prev slow 10; now fast (9+x)/2 == slow (18+x)/3 -> x = 9
        assert evaluate(SmaCross(fast=2, slow=3), closes(12, 9, 9, 9)) == []

    def test_output_text_does_not_depend_on_decimal_exponent(self) -> None:
        # Same values, different exponents (as read back from NUMERIC(38, 18)).
        plain = closes("10", "10", "10", "15")
        padded = [
            replace(
                c,
                open=c.open.quantize(Decimal("1E-18")),
                high=c.high.quantize(Decimal("1E-18")),
                low=c.low.quantize(Decimal("1E-18")),
                close=c.close.quantize(Decimal("1E-18")),
            )
            for c in plain
        ]

        assert evaluate(SmaCross(fast=2, slow=3), plain) == evaluate(
            SmaCross(fast=2, slow=3), padded
        )

    def test_uses_exact_decimals(self) -> None:
        candles = closes("0.1", "0.1", "0.1", "0.2")

        [proposal] = evaluate(SmaCross(fast=2, slow=3), candles)

        assert proposal.metadata["fast_sma"] == "0.15"


class CountingStrategy:
    """Test double: proposes BUY on every candle and records what it saw."""

    strategy_id = "counting"
    version = "1"
    parameters: Mapping[str, ParameterValue] = {}

    def __init__(self, lookback: int = 3, proposals: int = 1, raise_error: bool = False) -> None:
        self.lookback = lookback
        self.proposals = proposals
        self.raise_error = raise_error
        self.seen: list[tuple[Candle, ...]] = []

    def evaluate(self, context: StrategyContext) -> Sequence[SignalProposal]:
        self.seen.append(context.candles)
        if self.raise_error:
            raise ZeroDivisionError("boom")
        return [SignalProposal(Side.BUY, "always")] * self.proposals


class TestEvaluation:
    def test_window_problems(self) -> None:
        candles = closes(1, 2, 3, 4)

        assert window_problem(candles[:3], FIVE_MIN, 3) is None
        assert "needs 3" in str(window_problem(candles[:2], FIVE_MIN, 3))
        assert "not contiguous" in str(
            window_problem([candles[0], candles[2], candles[3]], FIVE_MIN, 3)
        )
        open_candle = make_candle(open_time=candles[2].open_time, is_closed=False, ingested_at=T0)
        assert "unfinished" in str(window_problem([*candles[:2], open_candle], FIVE_MIN, 3))
        assert "not 1m" in str(window_problem(candles[:3], Timeframe("1m"), 3))

    def test_engine_stamps_identity_from_the_candle(self) -> None:
        candles = closes(1, 2, 3)
        strategy = CountingStrategy()

        signal = evaluate_candle(
            strategy, window=candles, timeframe=FIVE_MIN, portfolio=EMPTY, created_at=T0
        )

        assert signal is not None
        assert signal.strategy_id == "counting"
        assert signal.config_hash == config_of(strategy).config_hash
        assert signal.source_candle_time == candles[-1].open_time
        assert (signal.exchange_id, signal.symbol) == ("binance", "BTC/USDT")

    def test_more_than_one_proposal_is_a_contract_break(self) -> None:
        with pytest.raises(StrategyError, match="at most 1"):
            evaluate_candle(
                CountingStrategy(proposals=2),
                window=closes(1, 2, 3),
                timeframe=FIVE_MIN,
                portfolio=EMPTY,
                created_at=T0,
            )

    def test_strategy_exceptions_are_wrapped(self) -> None:
        with pytest.raises(StrategyError, match="raised while evaluating") as exc_info:
            evaluate_candle(
                CountingStrategy(raise_error=True),
                window=closes(1, 2, 3),
                timeframe=FIVE_MIN,
                portfolio=EMPTY,
                created_at=T0,
            )
        assert isinstance(exc_info.value.__cause__, ZeroDivisionError)

    def test_invalid_window_is_refused_before_calling_strategy(self) -> None:
        strategy = CountingStrategy()

        with pytest.raises(StrategyError, match="invalid evaluation window"):
            evaluate_candle(
                strategy, window=closes(1, 2), timeframe=FIVE_MIN, portfolio=EMPTY, created_at=T0
            )
        assert strategy.seen == []


SERIES = closes(10, 11, 12, 11, 10, 9, 10, 12, 14, 13, 11, 9, 8, 9, 11, 13, 15, 14, 12, 10)


class TestReplay:
    def test_warm_up_candles_are_not_evaluated(self) -> None:
        strategy = CountingStrategy(lookback=3)

        signals = list(replay(strategy, SERIES, timeframe=FIVE_MIN, portfolio=EMPTY))

        assert len(signals) == len(SERIES) - 2
        assert signals[0].source_candle_time == SERIES[2].open_time
        assert all(len(window) == 3 for window in strategy.seen)

    def test_gaps_skip_candles_without_a_full_window(self) -> None:
        with_gap = SERIES[:8] + SERIES[9:]
        strategy = CountingStrategy(lookback=3)

        signals = list(replay(strategy, with_gap, timeframe=FIVE_MIN, portfolio=EMPTY))
        times = {s.source_candle_time for s in signals}

        # The two candles after the gap lack a contiguous 3-candle window.
        assert SERIES[9].open_time not in times
        assert SERIES[10].open_time not in times
        assert SERIES[11].open_time in times

    def test_deterministic(self) -> None:
        strategy = SmaCross(fast=2, slow=4)

        first = list(replay(strategy, SERIES, timeframe=FIVE_MIN, portfolio=EMPTY))
        second = list(replay(SmaCross(fast=2, slow=4), SERIES, timeframe=FIVE_MIN, portfolio=EMPTY))

        assert first == second
        assert len(first) >= 2  # the series is built to cross both ways
        assert {s.side for s in first} == {Side.BUY, Side.SELL}
        close_times = {c.open_time: c.close_time for c in SERIES}
        assert all(s.created_at == close_times[s.source_candle_time] for s in first)

    def test_no_lookahead(self) -> None:
        # Signals up to candle k must not change when later candles are added or altered.
        full = list(replay(SmaCross(fast=2, slow=4), SERIES, timeframe=FIVE_MIN, portfolio=EMPTY))
        for k in range(5, len(SERIES)):
            prefix = list(
                replay(SmaCross(fast=2, slow=4), SERIES[:k], timeframe=FIVE_MIN, portfolio=EMPTY)
            )
            cutoff = SERIES[k].open_time
            assert prefix == [s for s in full if s.source_candle_time < cutoff]

    def test_strategy_never_sees_beyond_the_current_candle(self) -> None:
        strategy = CountingStrategy(lookback=3)

        list(replay(strategy, SERIES, timeframe=FIVE_MIN, portfolio=EMPTY))

        for index, window in enumerate(strategy.seen, start=2):
            assert window[-1] is SERIES[index]


class TestRegistry:
    def test_builds_sma_cross(self) -> None:
        strategy = build_strategy("sma_cross", "1", {"fast": 10, "slow": 30})

        assert strategy.lookback == 31

    def test_unknown_strategy(self) -> None:
        with pytest.raises(UnknownStrategyError, match="known"):
            build_strategy("nope", "1", {})

    def test_version_must_match_code(self) -> None:
        with pytest.raises(StrategyConfigError, match="review the change"):
            build_strategy("sma_cross", "2", {"fast": 10, "slow": 30})

    def test_bad_parameters(self) -> None:
        with pytest.raises(StrategyConfigError, match="invalid parameters"):
            build_strategy("sma_cross", "1", {"fast": 30, "slow": 10})

    def test_fixture_strategy_refused_in_live(self) -> None:
        with pytest.raises(StrategyConfigError, match="not allowed in live"):
            build_strategy("sma_cross", "1", {"fast": 10, "slow": 30}, live=True)


def test_strategy_protocol_is_satisfied() -> None:
    from arbitron.strategy.base import Strategy  # noqa: PLC0415

    strategies: list[Strategy] = [SmaCross(fast=2, slow=3), CountingStrategy()]
    assert all(s.lookback >= 1 for s in strategies)
