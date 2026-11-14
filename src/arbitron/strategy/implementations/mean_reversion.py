"""RSI mean reversion: buy a sharp fall, sell the recovery.

Hypothesis: after a sharp fall, price tends to bounce back toward its recent
level. It works in ranging markets and loses in sustained crashes, because it
keeps buying a falling price.

Rule on each finalized candle, with Wilder's RSI over ``length`` closes:
- ``buy``  when RSI < ``oversold``;
- ``sell`` when RSI > ``exit_level``;
- nothing otherwise.

The sizer ignores a buy while holding and a sell while flat. Research
candidate: refused in live mode.
"""

from collections.abc import Mapping, Sequence

from arbitron.domain.enums import Side
from arbitron.domain.money import canonical_str
from arbitron.domain.strategies import ParameterValue
from arbitron.strategy.base import SignalProposal, StrategyContext
from arbitron.strategy.implementations._support import (
    StrategyParameterError,
    int_parameters,
    positive,
    rsi,
)

STRATEGY_ID = "mean_reversion"
VERSION = "1"


class MeanReversion:
    strategy_id = STRATEGY_ID
    version = VERSION
    allowed_in_live = False  # research candidate

    def __init__(self, *, length: int, oversold: int, exit_level: int) -> None:
        positive("length", length)
        if not 0 < oversold < exit_level < 100:
            msg = f"need 0 < oversold ({oversold}) < exit_level ({exit_level}) < 100"
            raise StrategyParameterError(msg)
        self.length = length
        self.oversold = oversold
        self.exit_level = exit_level

    @classmethod
    def from_parameters(cls, parameters: Mapping[str, ParameterValue]) -> "MeanReversion":
        return cls(**int_parameters(STRATEGY_ID, parameters, ["length", "oversold", "exit_level"]))

    @property
    def parameters(self) -> Mapping[str, ParameterValue]:
        return {"length": self.length, "oversold": self.oversold, "exit_level": self.exit_level}

    @property
    def lookback(self) -> int:
        # RSI is seeded with ``length`` changes, then smoothed over a further
        # ``length`` so the seed's influence has mostly faded. Fixed, so the
        # value never depends on how much history happens to be stored.
        return 2 * self.length + 1

    def evaluate(self, context: StrategyContext) -> Sequence[SignalProposal]:
        value = rsi([candle.close for candle in context.candles], self.length)
        if value < self.oversold:
            side, reason = Side.BUY, "rsi_oversold"
        elif value > self.exit_level:
            side, reason = Side.SELL, "rsi_recovered"
        else:
            return []
        return [
            SignalProposal(
                side=side,
                reason_code=reason,
                explanation=f"RSI({self.length}) = {canonical_str(value)}",
                metadata={"rsi": canonical_str(value)},
            )
        ]
