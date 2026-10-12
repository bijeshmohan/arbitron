from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType

from arbitron.domain._checks import require_non_negative, require_text, require_utc
from arbitron.domain.errors import DomainValidationError
from arbitron.domain.positions import Position


@dataclass(frozen=True, slots=True)
class Balance:
    asset: str
    free: Decimal
    used: Decimal
    total: Decimal
    observed_at: datetime

    def __post_init__(self) -> None:
        require_text("asset", self.asset)
        require_non_negative("free", self.free)
        require_non_negative("used", self.used)
        require_non_negative("total", self.total)
        require_utc("observed_at", self.observed_at)
        if self.free + self.used != self.total:
            msg = f"{self.asset}: free {self.free} + used {self.used} != total {self.total}"
            raise DomainValidationError(msg)


@dataclass(frozen=True, slots=True)
class PortfolioView:
    """Read-only portfolio snapshot handed to strategies, the sizer, and risk.

    Keyed by asset (balances) and unified symbol (positions).
    """

    as_of: datetime
    balances: Mapping[str, Balance] = field(default_factory=lambda: MappingProxyType({}))
    positions: Mapping[str, Position] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        require_utc("as_of", self.as_of)
        for asset, balance in self.balances.items():
            if asset != balance.asset:
                msg = f"balance keyed {asset!r} is for {balance.asset!r}"
                raise DomainValidationError(msg)
        for symbol, position in self.positions.items():
            if symbol != position.symbol:
                msg = f"position keyed {symbol!r} is for {position.symbol!r}"
                raise DomainValidationError(msg)
        object.__setattr__(self, "balances", MappingProxyType(dict(self.balances)))
        object.__setattr__(self, "positions", MappingProxyType(dict(self.positions)))

    def free_balance(self, asset: str) -> Decimal:
        balance = self.balances.get(asset)
        return balance.free if balance is not None else Decimal(0)

    def position_quantity(self, symbol: str) -> Decimal:
        position = self.positions.get(symbol)
        return position.quantity if position is not None else Decimal(0)


@dataclass(frozen=True, slots=True)
class EquitySnapshot:
    """Portfolio equity at one instant, valued per plan §11.1.1 (last closed candles).

    The source of the daily-loss baseline (first snapshot of the UTC day) and
    the drawdown high-water mark (highest snapshot).
    """

    taken_at: datetime
    quote_currency: str
    equity: Decimal
    # asset -> quote value, as exact decimal strings
    components: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    unvalued_assets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_utc("taken_at", self.taken_at)
        require_text("quote_currency", self.quote_currency)
        require_non_negative("equity", self.equity)
        object.__setattr__(self, "components", MappingProxyType(dict(self.components)))
