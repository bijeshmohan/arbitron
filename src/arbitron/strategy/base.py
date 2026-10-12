"""Strategy interface (plan §10.1).

Strategies are synchronous, pure functions of an immutable context. They cannot
await anything, so they cannot perform I/O; they receive no exchange, database,
or network handles; and they return direction-only proposals. The engine, not
the strategy, stamps identity (strategy, version, config, market, candle time)
onto the resulting signals.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

from arbitron.domain.enums import Side
from arbitron.domain.errors import DomainValidationError
from arbitron.domain.market import Candle, Timeframe
from arbitron.domain.portfolio import PortfolioView
from arbitron.domain.strategies import ParameterValue


class StrategyError(Exception):
    """A strategy failed or broke its contract. Evaluation stops for that market."""


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Everything a strategy may see when evaluating one finalized candle.

    ``candles`` is exactly ``lookback`` contiguous closed candles, oldest first,
    ending with the candle being evaluated. Nothing after that candle is ever
    included (no lookahead).
    """

    exchange_id: str
    symbol: str
    timeframe: Timeframe
    candles: tuple[Candle, ...]
    portfolio: PortfolioView

    @property
    def current(self) -> Candle:
        return self.candles[-1]


@dataclass(frozen=True, slots=True)
class SignalProposal:
    """A strategy's output: a direction and why. No quantity, no identity."""

    side: Side
    reason_code: str
    explanation: str = ""
    metadata: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not self.reason_code.strip():
            msg = "reason_code must be a non-empty string"
            raise DomainValidationError(msg)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


class Strategy(Protocol):
    @property
    def strategy_id(self) -> str: ...

    @property
    def version(self) -> str:
        """Bump whenever behavior changes, so old and new signals stay distinguishable."""
        ...

    @property
    def parameters(self) -> Mapping[str, ParameterValue]: ...

    @property
    def lookback(self) -> int:
        """Closed candles required per evaluation, including the current one."""
        ...

    def evaluate(self, context: StrategyContext) -> Sequence[SignalProposal]:
        """At most one proposal per candle. Must be deterministic and side-effect free.

        Format any Decimal placed in ``explanation`` or ``metadata`` with
        ``arbitron.domain.money.canonical_str``: candles read from storage carry
        a different exponent than equal in-memory values, and the signal text
        must not depend on where the candles came from.
        """
        ...
