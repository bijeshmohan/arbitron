from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from arbitron.domain._checks import (
    require_non_negative,
    require_positive,
    require_text,
    require_utc,
)
from arbitron.domain.enums import Side


@dataclass(frozen=True, slots=True)
class Fee:
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        require_non_negative("fee amount", self.amount)
        require_text("fee currency", self.currency)


@dataclass(frozen=True, slots=True)
class ExchangeTrade:
    """A trade (execution) as reported by the venue, before it is linked to a local order."""

    exchange_id: str
    trade_id: str | None
    exchange_order_id: str
    symbol: str
    side: Side
    amount: Decimal
    price: Decimal
    fee: Fee | None
    timestamp: datetime

    def __post_init__(self) -> None:
        require_text("exchange_id", self.exchange_id)
        if self.trade_id is not None:
            require_text("trade_id", self.trade_id)
        require_text("exchange_order_id", self.exchange_order_id)
        require_text("symbol", self.symbol)
        require_positive("amount", self.amount)
        require_positive("price", self.price)
        require_utc("timestamp", self.timestamp)

    @property
    def dedupe_key(self) -> str:
        """Venue trade ID when available; otherwise a composite of the trade's facts.

        The composite fallback can merge two genuinely distinct trades with
        identical order, time, amount, and price. Venues without trade IDs are
        flagged by capability checks and reconciled conservatively.
        """
        if self.trade_id is not None:
            return f"{self.exchange_id}:trade:{self.trade_id}"
        return ":".join(
            (
                self.exchange_id,
                "order",
                self.exchange_order_id,
                self.timestamp.isoformat(),
                str(self.amount.normalize()),
                str(self.price.normalize()),
            )
        )


@dataclass(frozen=True, slots=True)
class Fill:
    """An immutable execution fact linked to a local order."""

    order_id: UUID
    trade: ExchangeTrade

    @property
    def dedupe_key(self) -> str:
        return self.trade.dedupe_key
