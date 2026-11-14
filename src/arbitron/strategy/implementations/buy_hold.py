"""Buy and hold: the baseline every other strategy has to beat.

Buys when flat and never sells. It exists so a candidate's result can be judged
against doing nothing clever, in the same engine with the same costs. Not a
recommendation, and refused in live mode.
"""

from collections.abc import Mapping, Sequence

from arbitron.domain.enums import Side
from arbitron.domain.strategies import ParameterValue
from arbitron.strategy.base import SignalProposal, StrategyContext
from arbitron.strategy.implementations._support import int_parameters

STRATEGY_ID = "buy_hold"
VERSION = "1"


class BuyHold:
    strategy_id = STRATEGY_ID
    version = VERSION
    allowed_in_live = False  # research baseline only

    @classmethod
    def from_parameters(cls, parameters: Mapping[str, ParameterValue]) -> "BuyHold":
        int_parameters(STRATEGY_ID, parameters, [])
        return cls()

    @property
    def parameters(self) -> Mapping[str, ParameterValue]:
        return {}

    @property
    def lookback(self) -> int:
        return 1

    def evaluate(self, context: StrategyContext) -> Sequence[SignalProposal]:
        # Repeated every candle; the sizer ignores it while the position is held.
        return [SignalProposal(side=Side.BUY, reason_code="hold_baseline")]
