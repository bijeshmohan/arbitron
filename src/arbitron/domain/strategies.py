"""Strategy configuration snapshots and evaluation cursors (plan §10.2)."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType

from arbitron.domain._checks import require_text, require_utc
from arbitron.domain.errors import DomainValidationError
from arbitron.domain.market import Timeframe

# Floats are excluded for the same reason as everywhere else: parameters that
# feed financial calculations must be exact. Pass decimals as strings.
type ParameterValue = str | int | bool


def _parameter(key: str, value: object) -> ParameterValue:
    """Validate one parameter; values arrive from untyped config files."""
    require_text("parameter name", key)
    if not isinstance(value, str | int | bool):
        msg = (
            f"parameter {key!r} must be str, int, or bool (use a string for decimals), "
            f"got {type(value).__name__}"
        )
        raise DomainValidationError(msg)
    return value


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """An immutable snapshot of what was run: strategy, code version, and parameters.

    ``config_hash`` identifies the snapshot. It is part of every signal's
    identity, so changing a parameter (even without bumping the version)
    produces a distinct signal stream instead of colliding with the old one.
    """

    strategy_id: str
    version: str
    parameters: Mapping[str, ParameterValue] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        require_text("strategy_id", self.strategy_id)
        require_text("version", self.version)
        frozen = {key: _parameter(key, value) for key, value in self.parameters.items()}
        object.__setattr__(self, "parameters", MappingProxyType(frozen))

    @property
    def config_hash(self) -> str:
        canonical = json.dumps(
            {
                "strategy_id": self.strategy_id,
                "version": self.version,
                "parameters": dict(self.parameters),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class StrategyCursor:
    """The last finalized candle a strategy config has evaluated for one market."""

    config_hash: str
    exchange_id: str
    symbol: str
    timeframe: Timeframe
    last_open_time: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        for name in ("config_hash", "exchange_id", "symbol"):
            require_text(name, getattr(self, name))
        require_utc("last_open_time", self.last_open_time)
        require_utc("updated_at", self.updated_at)
