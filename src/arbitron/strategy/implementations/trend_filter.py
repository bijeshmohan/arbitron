"""Trend filter: hold while the price is above its long moving average.

Hypothesis: crypto trends persist for weeks, so staying in only while the
close is above its ``length``-candle average avoids much of each large fall,
at the price of whipsaw losses in sideways markets.

Rule on each finalized candle, using closes:
- ``buy``  when close > SMA(length);
- ``sell`` when close < SMA(length);
- nothing when they are equal.

This is a state rule, so it repeats the same signal while the state lasts. The
sizer ignores a buy while holding and a sell while flat. Research candidate:
refused in live mode.
"""

from collections.abc import Mapping, Sequence

from arbitron.domain.enums import Side
from arbitron.domain.money import canonical_str
from arbitron.domain.strategies import ParameterValue
from arbitron.strategy.base import SignalProposal, StrategyContext
from arbitron.strategy.implementations._support import int_parameters, positive, sma

STRATEGY_ID = "trend_filter"
VERSION = "1"


class TrendFilter:
    strategy_id = STRATEGY_ID
    version = VERSION
    allowed_in_live = False  # research candidate

    def __init__(self, *, length: int) -> None:
        positive("length", length)
        self.length = length

    @classmethod
    def from_parameters(cls, parameters: Mapping[str, ParameterValue]) -> "TrendFilter":
        return cls(**int_parameters(STRATEGY_ID, parameters, ["length"]))

    @property
    def parameters(self) -> Mapping[str, ParameterValue]:
        return {"length": self.length}

    @property
    def lookback(self) -> int:
        return self.length

    def evaluate(self, context: StrategyContext) -> Sequence[SignalProposal]:
        closes = [candle.close for candle in context.candles]
        average = sma(closes, self.length)
        close = closes[-1]
        if close > average:
            side, reason = Side.BUY, "close_above_sma"
        elif close < average:
            side, reason = Side.SELL, "close_below_sma"
        else:
            return []
        return [
            SignalProposal(
                side=side,
                reason_code=reason,
                explanation=f"close {canonical_str(close)} vs SMA({self.length}) "
                f"{canonical_str(average)}",
                metadata={"close": canonical_str(close), "sma": canonical_str(average)},
            )
        ]
