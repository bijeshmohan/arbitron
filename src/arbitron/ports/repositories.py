"""Persistence ports (implemented in Phase 2 on async SQLAlchemy/PostgreSQL).

All repositories are used inside a ``UnitOfWork``; nothing is durable until
``commit()``. Leaving the unit of work without committing rolls back.

Idempotent inserts return ``False`` instead of raising when an identical row
already exists, so replays and retries collapse onto the existing record. If
the existing row has the same identity but different content, they raise
``ConflictingRecordError``: that signals nondeterminism or a venue
discrepancy and must never be silently ignored.
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from arbitron.domain.enums import Severity
from arbitron.domain.events import OrderEvent, SystemEvent
from arbitron.domain.experiments import ExperimentRecord
from arbitron.domain.fills import Fill
from arbitron.domain.market import Candle, Market, Timeframe
from arbitron.domain.orders import Order
from arbitron.domain.portfolio import EquitySnapshot
from arbitron.domain.positions import Position
from arbitron.domain.risk import KillSwitch, RiskDecision
from arbitron.domain.signals import Signal
from arbitron.domain.strategies import StrategyConfig, StrategyCursor


class ConcurrencyConflict(Exception):  # noqa: N818 - domain term, not a generic error
    """The stored state no longer matches the state the caller transitioned from."""


class ConflictingRecordError(Exception):
    """A record with the same identity exists with different content, or a
    write-once record was written twice."""


class MarketRepository(Protocol):
    async def save(self, market: Market, *, observed_at: datetime) -> None:
        """Insert or replace the latest known rules for this market."""
        ...

    async def get(self, exchange_id: str, symbol: str) -> Market | None: ...


class ExperimentRepository(Protocol):
    async def add(self, record: ExperimentRecord) -> None: ...

    async def get(self, experiment_id: UUID) -> ExperimentRecord | None: ...

    async def list_recent(self, limit: int) -> Sequence[ExperimentRecord]: ...


class CandleRepository(Protocol):
    async def upsert(self, candles: Sequence[Candle]) -> int:
        """Insert or update by candle identity; a stored closed candle is never
        modified or reopened. Returns the number of rows written.

        Detecting a closed candle that the venue later revised is the market
        data subsystem's job (compare before upsert).
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


class StrategyConfigRepository(Protocol):
    async def add_if_absent(self, config: StrategyConfig, *, created_at: datetime) -> bool:
        """Write-once snapshot keyed by ``config.config_hash``."""
        ...

    async def get(self, config_hash: str) -> StrategyConfig | None: ...


class StrategyCursorRepository(Protocol):
    async def get(
        self, config_hash: str, exchange_id: str, symbol: str, timeframe: Timeframe
    ) -> StrategyCursor | None: ...

    async def save(self, previous: StrategyCursor | None, updated: StrategyCursor) -> None:
        """Advance the cursor. Raises ``ConcurrencyConflict`` if ``previous`` is stale,
        and ``ValueError`` if it would move backwards or change identity."""
        ...


class SignalRepository(Protocol):
    async def add_if_absent(self, signal: Signal) -> bool:
        """Compared on everything except ``created_at`` when a duplicate exists."""
        ...

    async def get(self, signal_id: UUID) -> Signal | None: ...

    async def latest_for(
        self, config_hash: str, exchange_id: str, symbol: str, timeframe: Timeframe
    ) -> Signal | None:
        """The signal with the latest source candle for this config and market."""
        ...


class OrderRepository(Protocol):
    async def count_created_since(self, exchange_id: str, since: datetime) -> int:
        """Orders whose intent was created at or after ``since`` (order-frequency limit)."""
        ...

    async def list_events(self, order_id: UUID) -> Sequence[OrderEvent]:
        """Lifecycle events in insertion order."""
        ...

    async def add_if_absent(self, order: Order, event: OrderEvent) -> bool:
        """Persist a new ``CREATED`` order and its creating event.

        A duplicate intent is compared on everything except ``created_at``.
        """
        ...

    async def get(self, order_id: UUID) -> Order | None: ...

    async def get_by_exchange_order_id(
        self, exchange_id: str, exchange_order_id: str
    ) -> Order | None: ...

    async def get_by_client_order_id(
        self, exchange_id: str, client_order_id: str
    ) -> Order | None: ...

    async def list_non_terminal(self, exchange_id: str) -> Sequence[Order]: ...

    async def list_recent(self, exchange_id: str, limit: int) -> Sequence[Order]:
        """Most recently created first."""
        ...

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

    async def list_for_order(self, order_id: UUID) -> Sequence[Fill]:
        """Ascending by trade timestamp."""
        ...


class RiskDecisionRepository(Protocol):
    async def add(self, decision: RiskDecision) -> None:
        """Write-once per intent; a second decision raises ``ConflictingRecordError``."""
        ...

    async def get_for_intent(self, intent_id: UUID) -> RiskDecision | None: ...


class KillSwitchRepository(Protocol):
    async def get(self) -> KillSwitch | None:
        """Current state; ``None`` only before first initialization."""
        ...

    async def save(self, previous: KillSwitch | None, updated: KillSwitch) -> None:
        """Persist and record history. Raises ``ConcurrencyConflict`` if stale."""
        ...


class PositionRepository(Protocol):
    async def get(self, exchange_id: str, symbol: str) -> Position | None: ...

    async def list(self, exchange_id: str) -> Sequence[Position]: ...

    async def save(self, previous: Position | None, updated: Position) -> None:
        """Raises ``ConcurrencyConflict`` if the stored position is not ``previous``."""
        ...


class EquitySnapshotRepository(Protocol):
    async def add(self, snapshot: EquitySnapshot) -> None: ...

    async def first_since(self, quote_currency: str, since: datetime) -> EquitySnapshot | None:
        """Earliest snapshot at or after ``since`` (the daily-loss baseline)."""
        ...

    async def peak(self, quote_currency: str) -> Decimal | None:
        """Highest equity ever recorded (the drawdown high-water mark)."""
        ...

    async def latest(self, quote_currency: str) -> EquitySnapshot | None: ...


class ReconciliationLookupRepository(Protocol):
    async def record(self, order_id: UUID, *, checked_at: datetime, found: bool) -> None: ...

    async def not_found_since(self, order_id: UUID, since: datetime) -> int:
        """Not-found lookups at or after ``since``, counted only if no lookup since then
        found the order (i.e. consecutive misses)."""
        ...


class SystemEventRepository(Protocol):
    async def append(self, event: SystemEvent) -> None: ...

    async def latest_id(self) -> int:
        """Position of the newest event (0 if none): a cursor for readers."""
        ...

    async def list_after(
        self, after_id: int, severities: frozenset[Severity], limit: int
    ) -> Sequence[tuple[int, SystemEvent]]:
        """Events newer than ``after_id`` with the given severities, oldest first."""
        ...

    async def list_recent(self, limit: int) -> Sequence[SystemEvent]:
        """Most recent first."""
        ...


class HeartbeatRepository(Protocol):
    async def upsert(
        self, *, component: str, instance: str, beat_at: datetime, status: str, detail: str
    ) -> None: ...


class UnitOfWork(Protocol):
    markets: MarketRepository
    experiments: ExperimentRepository
    candles: CandleRepository
    strategy_configs: StrategyConfigRepository
    strategy_cursors: StrategyCursorRepository
    signals: SignalRepository
    orders: OrderRepository
    fills: FillRepository
    risk_decisions: RiskDecisionRepository
    kill_switch: KillSwitchRepository
    positions: PositionRepository
    equity_snapshots: EquitySnapshotRepository
    reconciliation_lookups: ReconciliationLookupRepository
    heartbeats: HeartbeatRepository
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
