from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from uuid import UUID

from arbitron.domain._checks import require_text, require_utc
from arbitron.domain.enums import Side
from arbitron.domain.ids import deterministic_id
from arbitron.domain.market import Timeframe


@dataclass(frozen=True, slots=True)
class Signal:
    """A strategy's directional view on one finalized candle.

    A signal carries direction and rationale only, never a quantity: sizing is
    the sizer's job and approval is the risk engine's. A strategy config emits
    at most one signal per (config, market, timeframe, candle), which is what
    the dedupe key encodes.
    """

    strategy_id: str
    strategy_version: str
    config_hash: str  # StrategyConfig.config_hash: strategy + version + parameters
    exchange_id: str
    symbol: str
    timeframe: Timeframe
    side: Side
    source_candle_time: datetime  # open time of the finalized candle that produced it
    created_at: datetime
    reason_code: str
    explanation: str = ""
    # Informational only; never trusted by sizing or risk.
    metadata: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        for name in (
            "strategy_id",
            "strategy_version",
            "config_hash",
            "exchange_id",
            "symbol",
            "reason_code",
        ):
            require_text(name, getattr(self, name))
        require_utc("source_candle_time", self.source_candle_time)
        require_utc("created_at", self.created_at)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def dedupe_key(self) -> str:
        return ":".join(
            (
                self.strategy_id,
                self.strategy_version,
                self.config_hash,
                self.exchange_id,
                self.symbol,
                self.timeframe.value,
                self.source_candle_time.isoformat(),
            )
        )

    @property
    def id(self) -> UUID:
        return deterministic_id("signal", self.dedupe_key)
