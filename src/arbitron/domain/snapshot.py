"""The state that sizing and risk decisions are made from (plan §11)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType

from arbitron.domain._checks import require_positive, require_text, require_utc
from arbitron.domain.enums import OrderStatus, Side
from arbitron.domain.errors import DomainValidationError
from arbitron.domain.market import Market, Ticker
from arbitron.domain.orders import Order
from arbitron.domain.portfolio import Balance
from arbitron.domain.risk import KillSwitch

# Orders the venue has not acknowledged yet: their funds are not reflected in
# exchange balances, so they must be counted separately as committed.
UNACKNOWLEDGED_STATUSES = frozenset(
    {
        OrderStatus.CREATED,
        OrderStatus.RISK_APPROVED,
        OrderStatus.SUBMITTING,
        OrderStatus.SUBMISSION_UNKNOWN,
        OrderStatus.RECONCILIATION_REQUIRED,
    }
)


def _frozen[K, V](mapping: Mapping[K, V]) -> Mapping[K, V]:
    return MappingProxyType(dict(mapping))


@dataclass(frozen=True, slots=True)
class TradingSnapshot:
    """Everything the sizer and risk engine may read, captured at one instant.

    Built by the application layer (Phase 6) from the database, venue, and
    portfolio projection. Anything that could not be established is ``None``
    or ``False`` here, and the risk engine rejects on it (fail closed).

    - ``marks``: symbol -> last closed candle close (quote per base). The
      valuation source for equity, exposure, and loss limits.
    - ``tickers``: symbol -> latest ticker, for order pricing and spread checks.
    - ``open_orders``: every non-terminal local order.
    - ``day_start_equity``: equity at the start of the current UTC day.
    - ``peak_equity``: highest equity observed (drawdown high-water mark).
    """

    as_of: datetime
    quote_currency: str
    markets: Mapping[str, Market]
    marks: Mapping[str, Decimal]
    tickers: Mapping[str, Ticker]
    balances: Mapping[str, Balance]
    open_orders: tuple[Order, ...]
    orders_last_hour: int
    day_start_equity: Decimal | None
    peak_equity: Decimal | None
    market_data_ok: Mapping[str, bool]
    kill_switch: KillSwitch | None
    trading_enabled: bool
    system_ready: bool
    reconciliation_clean: bool
    exchange_healthy: bool
    # Free-form notes on how the snapshot was built (provenance), for audit.
    notes: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        require_utc("as_of", self.as_of)
        require_text("quote_currency", self.quote_currency)
        for symbol, market in self.markets.items():
            if symbol != market.symbol or market.quote != self.quote_currency:
                msg = f"market {symbol!r} does not belong to quote {self.quote_currency}"
                raise DomainValidationError(msg)
        for symbol, mark in self.marks.items():
            require_positive(f"mark[{symbol}]", mark)
        for order in self.open_orders:
            if order.is_terminal:
                msg = f"open_orders contains terminal order {order.id} ({order.status})"
                raise DomainValidationError(msg)
        if self.orders_last_hour < 0:
            msg = "orders_last_hour must not be negative"
            raise DomainValidationError(msg)
        for name in ("day_start_equity", "peak_equity"):
            value = getattr(self, name)
            if value is not None and value < 0:
                msg = f"{name} must not be negative"
                raise DomainValidationError(msg)
        for name in ("markets", "marks", "tickers", "balances", "market_data_ok", "notes"):
            object.__setattr__(self, name, _frozen(getattr(self, name)))

    def base_balance(self, symbol: str) -> Balance | None:
        market = self.markets.get(symbol)
        return self.balances.get(market.base) if market is not None else None

    def quote_balance(self) -> Balance | None:
        return self.balances.get(self.quote_currency)

    def open_orders_for(self, symbol: str) -> tuple[Order, ...]:
        return tuple(o for o in self.open_orders if o.intent.symbol == symbol)

    def committed_sells(self, symbol: str) -> Decimal:
        """Base quantity already promised to open SELL orders for ``symbol``."""
        return sum(
            (
                o.remaining_amount
                for o in self.open_orders_for(symbol)
                if o.intent.side is Side.SELL
            ),
            Decimal(0),
        )

    def held_quantity(self, symbol: str) -> Decimal:
        balance = self.base_balance(symbol)
        return balance.total if balance is not None else Decimal(0)
