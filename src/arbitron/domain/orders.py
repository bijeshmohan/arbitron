"""Order intents, order state, and the central order state machine (plan §6.2).

All status changes go through ``Order.transition``, which enforces both the
allowed-transition table and the status/fill-quantity invariants.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Final
from uuid import UUID

from arbitron.domain._checks import (
    require_non_negative,
    require_positive,
    require_text,
    require_utc,
)
from arbitron.domain.enums import OrderStatus, OrderType, Side, TimeInForce
from arbitron.domain.errors import DomainValidationError, InvalidTransitionError
from arbitron.domain.fills import Fee
from arbitron.domain.ids import client_order_id_for, deterministic_id

S = OrderStatus

TERMINAL_STATUSES: Final = frozenset(
    {
        S.RISK_REJECTED,
        S.EXCHANGE_REJECTED,
        S.NOT_PLACED,
        S.FILLED,
        S.CANCELED,
        S.EXPIRED,
        S.PARTIALLY_FILLED_CLOSED,
    }
)

_EXCHANGE_OUTCOMES: Final = frozenset(
    {
        S.OPEN,
        S.PARTIALLY_FILLED,
        S.FILLED,
        S.CANCELED,
        S.EXPIRED,
        S.PARTIALLY_FILLED_CLOSED,
        S.EXCHANGE_REJECTED,
        S.NOT_PLACED,
    }
)

_DECLARED: Final[dict[OrderStatus, frozenset[OrderStatus]]] = {
    S.CREATED: frozenset({S.RISK_APPROVED, S.RISK_REJECTED}),
    S.RISK_APPROVED: frozenset({S.SUBMITTING}),
    S.SUBMITTING: frozenset(
        {
            S.OPEN,
            S.PARTIALLY_FILLED,
            S.FILLED,
            S.CANCELED,
            S.EXPIRED,
            S.PARTIALLY_FILLED_CLOSED,
            S.EXCHANGE_REJECTED,
            S.SUBMISSION_UNKNOWN,
        }
    ),
    S.OPEN: frozenset(
        {
            S.PARTIALLY_FILLED,
            S.FILLED,
            S.CANCEL_REQUESTED,
            S.CANCELED,
            S.EXPIRED,
            S.PARTIALLY_FILLED_CLOSED,
        }
    ),
    S.PARTIALLY_FILLED: frozenset(
        {S.PARTIALLY_FILLED, S.FILLED, S.CANCEL_REQUESTED, S.PARTIALLY_FILLED_CLOSED}
    ),
    S.CANCEL_REQUESTED: frozenset(
        {
            S.CANCEL_REQUESTED,
            S.OPEN,
            S.PARTIALLY_FILLED,
            S.FILLED,
            S.CANCELED,
            S.EXPIRED,
            S.PARTIALLY_FILLED_CLOSED,
        }
    ),
    S.SUBMISSION_UNKNOWN: _EXCHANGE_OUTCOMES | {S.RECONCILIATION_REQUIRED},
    S.RECONCILIATION_REQUIRED: _EXCHANGE_OUTCOMES,
}


def _build_transitions() -> dict[OrderStatus, frozenset[OrderStatus]]:
    table: dict[OrderStatus, frozenset[OrderStatus]] = {}
    for status in OrderStatus:
        if status in TERMINAL_STATUSES:
            table[status] = frozenset()
            continue
        allowed = set(_DECLARED[status])
        # Any non-terminal state may escalate to reconciliation.
        if status is not S.RECONCILIATION_REQUIRED:
            allowed.add(S.RECONCILIATION_REQUIRED)
        table[status] = frozenset(allowed)
    return table


ALLOWED_TRANSITIONS: Final[Mapping[OrderStatus, frozenset[OrderStatus]]] = MappingProxyType(
    _build_transitions()
)

# Statuses a venue can report; the rest are local bookkeeping states.
VENUE_REPORTABLE_STATUSES: Final = frozenset(
    {
        S.OPEN,
        S.PARTIALLY_FILLED,
        S.FILLED,
        S.CANCELED,
        S.EXPIRED,
        S.PARTIALLY_FILLED_CLOSED,
        S.EXCHANGE_REJECTED,
    }
)

_ZERO_FILL: Final = frozenset(
    {
        S.CREATED,
        S.RISK_APPROVED,
        S.RISK_REJECTED,
        S.SUBMITTING,
        S.OPEN,
        S.CANCELED,
        S.EXPIRED,
        S.EXCHANGE_REJECTED,
        S.NOT_PLACED,
    }
)
_PARTIAL_FILL: Final = frozenset({S.PARTIALLY_FILLED, S.PARTIALLY_FILLED_CLOSED})
_PRE_SUBMISSION: Final = frozenset({S.CREATED, S.RISK_APPROVED, S.RISK_REJECTED, S.SUBMITTING})


def can_transition(current: OrderStatus, target: OrderStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def is_terminal(status: OrderStatus) -> bool:
    return status in TERMINAL_STATUSES


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """An immutable, sized request to trade, produced by the sizer.

    ``attempt`` distinguishes a deliberate new attempt for the same signal (for
    example after ``NOT_PLACED``); it changes the intent ID and therefore the
    client order ID, so a retry can never be confused with the original.
    """

    signal_id: UUID
    strategy_id: str
    exchange_id: str
    symbol: str
    side: Side
    order_type: OrderType
    amount: Decimal  # already rounded down to exchange precision
    limit_price: Decimal | None
    time_in_force: TimeInForce | None
    created_at: datetime
    sizing_policy: str
    sizing_inputs: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    attempt: int = 1

    def __post_init__(self) -> None:
        for name in ("strategy_id", "exchange_id", "symbol", "sizing_policy"):
            require_text(name, getattr(self, name))
        require_positive("amount", self.amount)
        require_utc("created_at", self.created_at)
        if self.order_type is OrderType.LIMIT:
            if self.limit_price is None:
                msg = "limit orders require a limit_price"
                raise DomainValidationError(msg)
            require_positive("limit_price", self.limit_price)
        elif self.limit_price is not None:
            msg = "market orders must not carry a limit_price"
            raise DomainValidationError(msg)
        if self.attempt < 1:
            msg = f"attempt must be >= 1, got {self.attempt}"
            raise DomainValidationError(msg)
        object.__setattr__(self, "sizing_inputs", MappingProxyType(dict(self.sizing_inputs)))

    @property
    def dedupe_key(self) -> str:
        return f"{self.signal_id}:{self.attempt}"

    @property
    def id(self) -> UUID:
        return deterministic_id("order_intent", self.dedupe_key)

    @property
    def client_order_id(self) -> str:
        return client_order_id_for(self.id)


@dataclass(frozen=True, slots=True)
class Order:
    """Current state of one intent's order. Change it only via ``transition``."""

    intent: OrderIntent
    status: OrderStatus
    updated_at: datetime
    exchange_order_id: str | None = None
    filled_amount: Decimal = Decimal(0)
    average_fill_price: Decimal | None = None

    def __post_init__(self) -> None:
        require_utc("updated_at", self.updated_at)
        require_non_negative("filled_amount", self.filled_amount)
        if self.exchange_order_id is not None:
            require_text("exchange_order_id", self.exchange_order_id)
            if self.status in _PRE_SUBMISSION:
                msg = f"{self.status} order cannot have an exchange_order_id"
                raise DomainValidationError(msg)
        if self.average_fill_price is not None:
            require_positive("average_fill_price", self.average_fill_price)
            if self.filled_amount == 0:
                msg = "average_fill_price requires a non-zero filled_amount"
                raise DomainValidationError(msg)
        self._check_fill_invariant()

    def _check_fill_invariant(self) -> None:
        filled, amount = self.filled_amount, self.intent.amount
        if filled > amount:
            msg = f"filled_amount {filled} exceeds order amount {amount}"
            raise DomainValidationError(msg)
        if self.status in _ZERO_FILL and filled != 0:
            msg = f"{self.status} order must have zero filled_amount, got {filled}"
            raise DomainValidationError(msg)
        if self.status in _PARTIAL_FILL and not 0 < filled < amount:
            msg = f"{self.status} order requires 0 < filled_amount < amount, got {filled}"
            raise DomainValidationError(msg)
        if self.status is S.FILLED and filled != amount:
            msg = f"filled order requires filled_amount == amount, got {filled} of {amount}"
            raise DomainValidationError(msg)
        if self.status is S.CANCEL_REQUESTED and filled == amount:
            msg = "cancel_requested order cannot be completely filled; use filled"
            raise DomainValidationError(msg)

    @classmethod
    def new(cls, intent: OrderIntent) -> "Order":
        return cls(intent=intent, status=S.CREATED, updated_at=intent.created_at)

    @property
    def id(self) -> UUID:
        return self.intent.id

    @property
    def remaining_amount(self) -> Decimal:
        return self.intent.amount - self.filled_amount

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.status)

    def transition(
        self,
        target: OrderStatus,
        *,
        at: datetime,
        filled_amount: Decimal | None = None,
        average_fill_price: Decimal | None = None,
        exchange_order_id: str | None = None,
    ) -> "Order":
        """Return the order in ``target`` state, or raise ``InvalidTransitionError``.

        Omitted fill fields keep their current values. Filled quantity never
        decreases, an exchange order ID never changes once known, and time never
        moves backwards.
        """
        if not can_transition(self.status, target):
            msg = f"order {self.id}: {self.status} -> {target} is not allowed"
            raise InvalidTransitionError(msg)
        require_utc("at", at)
        if at < self.updated_at:
            msg = f"order {self.id}: transition time {at} precedes last update {self.updated_at}"
            raise InvalidTransitionError(msg)
        new_filled = self.filled_amount if filled_amount is None else filled_amount
        if new_filled < self.filled_amount:
            msg = (
                f"order {self.id}: filled_amount cannot decrease "
                f"({self.filled_amount} -> {new_filled})"
            )
            raise InvalidTransitionError(msg)
        if (
            exchange_order_id is not None
            and self.exchange_order_id is not None
            and exchange_order_id != self.exchange_order_id
        ):
            msg = (
                f"order {self.id}: exchange_order_id cannot change "
                f"({self.exchange_order_id} -> {exchange_order_id})"
            )
            raise InvalidTransitionError(msg)
        try:
            return replace(
                self,
                status=target,
                updated_at=at,
                filled_amount=new_filled,
                average_fill_price=(
                    self.average_fill_price if average_fill_price is None else average_fill_price
                ),
                exchange_order_id=exchange_order_id or self.exchange_order_id,
            )
        except DomainValidationError as exc:
            msg = f"order {self.id}: {self.status} -> {target} violates invariants: {exc}"
            raise InvalidTransitionError(msg) from exc


@dataclass(frozen=True, slots=True)
class ExchangeOrder:
    """An order snapshot as reported by the venue, normalized by the adapter.

    ``raw_status`` keeps the venue's own status string because venue semantics
    differ; ``status`` is the adapter's mapping onto the internal enum.
    """

    exchange_id: str
    exchange_order_id: str
    client_order_id: str | None
    symbol: str
    side: Side
    order_type: OrderType
    status: OrderStatus
    raw_status: str
    amount: Decimal
    filled: Decimal
    remaining: Decimal
    average_price: Decimal | None
    fee: Fee | None
    timestamp: datetime

    def __post_init__(self) -> None:
        for name in ("exchange_id", "exchange_order_id", "symbol", "raw_status"):
            require_text(name, getattr(self, name))
        if self.status not in VENUE_REPORTABLE_STATUSES:
            msg = f"{self.status} is a local status and cannot be reported by a venue"
            raise DomainValidationError(msg)
        require_positive("amount", self.amount)
        require_non_negative("filled", self.filled)
        require_non_negative("remaining", self.remaining)
        if self.filled > self.amount:
            msg = f"filled {self.filled} exceeds amount {self.amount}"
            raise DomainValidationError(msg)
        if self.average_price is not None:
            require_positive("average_price", self.average_price)
        require_utc("timestamp", self.timestamp)
