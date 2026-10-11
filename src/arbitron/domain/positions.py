from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from arbitron.domain._checks import require_decimal, require_non_negative, require_text, require_utc
from arbitron.domain.errors import DomainValidationError


@dataclass(frozen=True, slots=True)
class Position:
    """A spot holding. Long-only in this release: quantity can never be negative.

    Accounting method (weighted average cost) is applied by the portfolio
    module; this entity only guards the invariants.
    """

    exchange_id: str
    symbol: str
    quantity: Decimal
    average_cost: Decimal
    realized_pnl: Decimal
    updated_at: datetime

    def __post_init__(self) -> None:
        require_text("exchange_id", self.exchange_id)
        require_text("symbol", self.symbol)
        require_non_negative("quantity", self.quantity)
        require_non_negative("average_cost", self.average_cost)
        require_decimal("realized_pnl", self.realized_pnl)
        require_utc("updated_at", self.updated_at)
        if self.quantity == 0 and self.average_cost != 0:
            msg = "a flat position must have zero average_cost"
            raise DomainValidationError(msg)

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0
