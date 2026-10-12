"""Simple moving-average crossover.

**A software integration fixture, not a trading recommendation.** It exists to
exercise the strategy -> signal -> sizing -> risk -> execution plumbing with a
deterministic, easily hand-checked rule. Nothing about it implies profitability
(plan §10.3).

Rule, evaluated on each finalized candle using closing prices:
- ``buy``  when the fast SMA crosses strictly above the slow SMA
  (previous fast <= previous slow, current fast > current slow);
- ``sell`` when the fast SMA crosses strictly below the slow SMA
  (previous fast >= previous slow, current fast < current slow);
- nothing otherwise, including when the averages are merely equal.
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal, localcontext

from arbitron.domain.enums import Side
from arbitron.domain.money import FINANCIAL_CONTEXT, canonical_str
from arbitron.domain.strategies import ParameterValue
from arbitron.strategy.base import SignalProposal, StrategyContext

STRATEGY_ID = "sma_cross"
VERSION = "1"


class SmaCrossParameterError(ValueError):
    pass


class SmaCross:
    strategy_id = STRATEGY_ID
    version = VERSION
    allowed_in_live = False  # integration fixture only

    def __init__(self, *, fast: int, slow: int) -> None:
        for name, value in (("fast", fast), ("slow", slow)):
            if isinstance(value, bool) or value < 1:
                msg = f"{name} must be a positive integer, got {value!r}"
                raise SmaCrossParameterError(msg)
        if fast >= slow:
            msg = f"fast ({fast}) must be smaller than slow ({slow})"
            raise SmaCrossParameterError(msg)
        self.fast = fast
        self.slow = slow

    @classmethod
    def from_parameters(cls, parameters: Mapping[str, ParameterValue]) -> "SmaCross":
        unknown = set(parameters) - {"fast", "slow"}
        if unknown:
            msg = f"unknown sma_cross parameters: {sorted(unknown)}"
            raise SmaCrossParameterError(msg)
        if "fast" not in parameters or "slow" not in parameters:
            msg = "sma_cross requires integer parameters 'fast' and 'slow'"
            raise SmaCrossParameterError(msg)
        fast, slow = parameters["fast"], parameters["slow"]
        # bool is an int subclass; reject it explicitly.
        if isinstance(fast, bool | str) or isinstance(slow, bool | str):
            msg = "sma_cross parameters 'fast' and 'slow' must be integers"
            raise SmaCrossParameterError(msg)
        return cls(fast=fast, slow=slow)

    @property
    def parameters(self) -> Mapping[str, ParameterValue]:
        return {"fast": self.fast, "slow": self.slow}

    @property
    def lookback(self) -> int:
        # The slow average now and one candle earlier.
        return self.slow + 1

    def evaluate(self, context: StrategyContext) -> Sequence[SignalProposal]:
        closes = [candle.close for candle in context.candles]
        previous, current = closes[:-1], closes
        fast_prev, slow_prev = _sma(previous, self.fast), _sma(previous, self.slow)
        fast_now, slow_now = _sma(current, self.fast), _sma(current, self.slow)

        if fast_prev <= slow_prev and fast_now > slow_now:
            side, reason = Side.BUY, "sma_fast_crossed_above_slow"
        elif fast_prev >= slow_prev and fast_now < slow_now:
            side, reason = Side.SELL, "sma_fast_crossed_below_slow"
        else:
            return []
        return [
            SignalProposal(
                side=side,
                reason_code=reason,
                explanation=(
                    f"SMA({self.fast}) moved from {canonical_str(fast_prev)} to "
                    f"{canonical_str(fast_now)}; SMA({self.slow}) moved from "
                    f"{canonical_str(slow_prev)} to {canonical_str(slow_now)}"
                ),
                metadata={
                    "fast_sma": canonical_str(fast_now),
                    "slow_sma": canonical_str(slow_now),
                    "fast_sma_previous": canonical_str(fast_prev),
                    "slow_sma_previous": canonical_str(slow_prev),
                },
            )
        ]


def _sma(values: Sequence[Decimal], length: int) -> Decimal:
    """Mean of the last ``length`` values, in the fixed financial Decimal context."""
    with localcontext(FINANCIAL_CONTEXT):
        return sum(values[-length:], Decimal(0)) / length
