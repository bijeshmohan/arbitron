"""Persistence ports (implemented in Phase 2 on async SQLAlchemy/PostgreSQL).

All repositories are used inside a ``UnitOfWork``; nothing is durable until
``commit()``. Leaving the unit of work without committing rolls back.

Idempotent inserts return ``False`` instead of raising when the row already
exists, so replays and retries collapse onto the existing record.
"""

from collections.abc import Sequence
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from arbitron.domain.events import OrderEvent, SystemEvent
from arbitron.domain.fills import Fill
from arbitron.domain.market import Candle, Timeframe
from arbitron.domain.orders import Order
from arbitron.domain.risk import KillSwitch, RiskDecision
from arbitron.domain.signals import Signal


class ConcurrencyConflict(Exception):  # noqa: N818 - domain term, not a generic error
    """The stored state no longer matches the state the caller transitioned from."""


class CandleRepository(Protocol):
    async def upsert(self, candles: Sequence[Candle]) -> int:
        """Insert or update by candle identity; a closed candle is never reopened.

        Returns the number of rows written.
        """
        ...

    async def latest_closed(
        self, exchange_id: str, symbol: str, timeframe: Timeframe
    ) -> Candle | None: ...

    async def closed_range(
        self,
        exchange_id: str,
        symbol: str,
        timeframe: Timeframe,
        *,
        start: datetime,
        end: datetime,
    ) -> Sequence[Candle]:
        """Closed candles with ``start <= open_time < end``, ascending."""
        ...


class SignalRepository(Protocol):
    async def add_if_absent(self, signal: Signal) -> bool: ...

    async def get(self, signal_id: UUID) -> Signal | None: ...


class OrderRepository(Protocol):
    async def add_if_absent(self, order: Order, event: OrderEvent) -> bool:
        """Persist a new ``CREATED`` order and its creating event."""
        ...

    async def get(self, order_id: UUID) -> Order | None: ...

    async def get_by_client_order_id(
        self, exchange_id: str, client_order_id: str
    ) -> Order | None: ...

    async def list_non_terminal(self, exchange_id: str) -> Sequence[Order]: ...

    async def apply_transition(self, previous: Order, updated: Order, event: OrderEvent) -> None:
        """Persist ``updated`` and append ``event`` atomically.

        Raises ``ConcurrencyConflict`` if the stored order is no longer in
        ``previous.status`` with ``previous.updated_at``.
        """
        ...


class FillRepository(Protocol):
    async def add_if_absent(self, fill: Fill) -> bool:
        """Deduplicated on ``fill.dedupe_key``."""
        ...

    async def list_for_order(self, order_id: UUID) -> Sequence[Fill]: ...


class RiskDecisionRepository(Protocol):
    async def add(self, decision: RiskDecision) -> None: ...


class KillSwitchRepository(Protocol):
    async def get(self) -> KillSwitch | None:
        """Current state; ``None`` only before first initialization."""
        ...

    async def save(self, previous: KillSwitch | None, updated: KillSwitch) -> None:
        """Persist and record history. Raises ``ConcurrencyConflict`` if stale."""
        ...


class SystemEventRepository(Protocol):
    async def append(self, event: SystemEvent) -> None: ...


class UnitOfWork(Protocol):
    candles: CandleRepository
    signals: SignalRepository
    orders: OrderRepository
    fills: FillRepository
    risk_decisions: RiskDecisionRepository
    kill_switch: KillSwitchRepository
    system_events: SystemEventRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...
