"""Backtest experiment records (plan §17.1: persist config, data range, versions, outputs)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from uuid import UUID

from arbitron.domain._checks import require_text, require_utc
from arbitron.domain.errors import DomainValidationError

type JsonScalar = str | int | bool | None


@dataclass(frozen=True, slots=True)
class ExperimentRecord:
    """One backtest run, immutable once recorded.

    ``data_fingerprint`` hashes every candle the run read, so two runs with
    the same fingerprint, config hash, policy version, assumptions, and code
    version must produce the same ``metrics``.
    """

    id: UUID
    created_at: datetime
    label: str  # e.g. "run", or "sweep:<id>:in_sample"
    exchange_id: str
    symbols: tuple[str, ...]
    timeframe: str
    start: datetime
    end: datetime
    strategy_id: str
    strategy_version: str
    config_hash: str
    risk_policy_version: str
    code_version: str
    data_fingerprint: str
    candle_count: int
    assumptions: Mapping[str, JsonScalar] = field(default_factory=lambda: MappingProxyType({}))
    parameters: Mapping[str, JsonScalar] = field(default_factory=lambda: MappingProxyType({}))
    metrics: Mapping[str, JsonScalar] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        for name in (
            "label",
            "exchange_id",
            "timeframe",
            "strategy_id",
            "strategy_version",
            "config_hash",
            "risk_policy_version",
            "code_version",
            "data_fingerprint",
        ):
            require_text(name, getattr(self, name))
        for name in ("created_at", "start", "end"):
            require_utc(name, getattr(self, name))
        if self.end <= self.start:
            msg = "experiment end must be after start"
            raise DomainValidationError(msg)
        if not self.symbols:
            msg = "experiment needs at least one symbol"
            raise DomainValidationError(msg)
        for name in ("assumptions", "parameters", "metrics"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))
