"""Channel breakout (Donchian): buy a new high, sell a new low.

Hypothesis: a close beyond the recent range marks the start of a larger move.
Rare, large moves pay for the many false breakouts.

Rule on each finalized candle, comparing the close with the ranges of the
candles *before* it:
- ``buy``  when close > highest high of the previous ``entry`` candles;
- ``sell`` when close < lowest low of the previous ``exit`` candles;
- nothing otherwise.

Use ``exit`` shorter than ``entry`` to leave faster than you arrive. The sizer
ignores a buy while holding and a sell while flat. Research candidate: refused
in live mode.
"""

from collections.abc import Mapping, Sequence

from arbitron.domain.enums import Side
from arbitron.domain.money import canonical_str
from arbitron.domain.strategies import ParameterValue
from arbitron.strategy.base import SignalProposal, StrategyContext
from arbitron.strategy.implementations._support import int_parameters, positive

STRATEGY_ID = "breakout"
VERSION = "1"


class Breakout:
    strategy_id = STRATEGY_ID
    version = VERSION
    allowed_in_live = False  # research candidate

    def __init__(self, *, entry: int, exit: int) -> None:
        positive("entry", entry)
        positive("exit", exit)
        self.entry = entry
        self.exit = exit

    @classmethod
    def from_parameters(cls, parameters: Mapping[str, ParameterValue]) -> "Breakout":
        return cls(**int_parameters(STRATEGY_ID, parameters, ["entry", "exit"]))

    @property
    def parameters(self) -> Mapping[str, ParameterValue]:
        return {"entry": self.entry, "exit": self.exit}

    @property
    def lookback(self) -> int:
        return max(self.entry, self.exit) + 1

    def evaluate(self, context: StrategyContext) -> Sequence[SignalProposal]:
        *earlier, current = context.candles
        high = max(c.high for c in earlier[-self.entry :])
        low = min(c.low for c in earlier[-self.exit :])
        if current.close > high:
            side, reason = Side.BUY, "close_above_channel_high"
        elif current.close < low:
            side, reason = Side.SELL, "close_below_channel_low"
        else:
            return []
        return [
            SignalProposal(
                side=side,
                reason_code=reason,
                explanation=f"close {canonical_str(current.close)}; "
                f"{self.entry}-candle high {canonical_str(high)}, "
                f"{self.exit}-candle low {canonical_str(low)}",
                metadata={
                    "close": canonical_str(current.close),
                    "channel_high": canonical_str(high),
                    "channel_low": canonical_str(low),
                },
            )
        ]
