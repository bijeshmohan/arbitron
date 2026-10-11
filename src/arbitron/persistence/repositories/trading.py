from collections.abc import Sequence
from dataclasses import replace
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from arbitron.domain.enums import OrderStatus
from arbitron.domain.events import OrderEvent
from arbitron.domain.fills import Fill
from arbitron.domain.orders import TERMINAL_STATUSES, Order, can_transition
from arbitron.domain.risk import RiskDecision
from arbitron.domain.signals import Signal
from arbitron.persistence.mapping import (
    fill_from_row,
    fill_values,
    order_event_from_row,
    order_event_values,
    order_from_row,
    order_state_values,
    order_values,
    risk_decision_from_row,
    risk_decision_values,
    signal_from_row,
    signal_values,
)
from arbitron.persistence.tables import (
    FillRow,
    OrderEventRow,
    OrderRow,
    RiskDecisionRow,
    SignalRow,
)
from arbitron.ports.repositories import ConcurrencyConflict, ConflictingRecordError


class SqlSignalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, signal: Signal) -> bool:
        stmt = (
            insert(SignalRow)
            .values(**signal_values(signal))
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
            .returning(SignalRow.id)
        )
        if await self._session.scalar(stmt) is not None:
            return True
        existing = await self.get(signal.id)
        # created_at reflects wall-clock processing time and may differ on replay.
        if existing is None or replace(existing, created_at=signal.created_at) != signal:
            msg = f"signal {signal.dedupe_key} already exists with different content"
            raise ConflictingRecordError(msg)
        return False

    async def get(self, signal_id: UUID) -> Signal | None:
        row = await self._session.get(SignalRow, signal_id)
        return signal_from_row(row) if row is not None else None


class SqlOrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, order: Order, event: OrderEvent) -> bool:
        if order.status is not OrderStatus.CREATED:
            msg = f"new orders must be persisted as created, not {order.status}"
            raise ValueError(msg)
        _check_event(event, order, from_status=None)
        stmt = (
            insert(OrderRow)
            .values(**order_values(order))
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
            .returning(OrderRow.id)
        )
        if await self._session.scalar(stmt) is None:
            existing = await self.get(order.id)
            # created_at reflects wall-clock processing time and may differ on replay.
            if (
                existing is None
                or replace(existing.intent, created_at=order.intent.created_at) != order.intent
            ):
                msg = (
                    f"order intent {order.intent.dedupe_key} already exists with different content"
                )
                raise ConflictingRecordError(msg)
            return False
        await self._session.execute(insert(OrderEventRow).values(**order_event_values(event)))
        return True

    async def get(self, order_id: UUID) -> Order | None:
        row = await self._session.get(OrderRow, order_id)
        return order_from_row(row) if row is not None else None

    async def get_by_client_order_id(self, exchange_id: str, client_order_id: str) -> Order | None:
        row = await self._session.scalar(
            select(OrderRow).where(
                OrderRow.exchange_id == exchange_id,
                OrderRow.client_order_id == client_order_id,
            )
        )
        return order_from_row(row) if row is not None else None

    async def list_non_terminal(self, exchange_id: str) -> Sequence[Order]:
        rows = await self._session.scalars(
            select(OrderRow)
            .where(
                OrderRow.exchange_id == exchange_id,
                OrderRow.status.not_in([s.value for s in TERMINAL_STATUSES]),
            )
            .order_by(OrderRow.intent_created_at, OrderRow.id)
        )
        return [order_from_row(row) for row in rows]

    async def list_events(self, order_id: UUID) -> Sequence[OrderEvent]:
        rows = await self._session.scalars(
            select(OrderEventRow)
            .where(OrderEventRow.order_id == order_id)
            .order_by(OrderEventRow.id)
        )
        return [order_event_from_row(row) for row in rows]

    async def apply_transition(self, previous: Order, updated: Order, event: OrderEvent) -> None:
        if updated.intent != previous.intent:
            msg = "a transition cannot change the order intent"
            raise ValueError(msg)
        if not can_transition(previous.status, updated.status):
            msg = f"{previous.status} -> {updated.status} is not an allowed transition"
            raise ValueError(msg)
        _check_event(event, updated, from_status=previous.status)
        stmt = (
            update(OrderRow)
            .where(
                OrderRow.id == previous.id,
                OrderRow.status == previous.status.value,
                OrderRow.updated_at == previous.updated_at,
            )
            .values(**order_state_values(updated))
            .returning(OrderRow.id)
        )
        if await self._session.scalar(stmt) is None:
            msg = f"order {previous.id} changed since it was read as {previous.status}"
            raise ConcurrencyConflict(msg)
        await self._session.execute(insert(OrderEventRow).values(**order_event_values(event)))


class SqlFillRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, fill: Fill) -> bool:
        stmt = (
            insert(FillRow)
            .values(**fill_values(fill))
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
            .returning(FillRow.id)
        )
        if await self._session.scalar(stmt) is not None:
            return True
        row = await self._session.scalar(
            select(FillRow).where(FillRow.dedupe_key == fill.dedupe_key)
        )
        if row is None or fill_from_row(row) != fill:
            msg = f"fill {fill.dedupe_key} already exists with different content"
            raise ConflictingRecordError(msg)
        return False

    async def list_for_order(self, order_id: UUID) -> Sequence[Fill]:
        rows = await self._session.scalars(
            select(FillRow)
            .where(FillRow.order_id == order_id)
            .order_by(FillRow.timestamp, FillRow.id)
        )
        return [fill_from_row(row) for row in rows]


class SqlRiskDecisionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, decision: RiskDecision) -> None:
        stmt = (
            insert(RiskDecisionRow)
            .values(**risk_decision_values(decision))
            .on_conflict_do_nothing(index_elements=["intent_id"])
            .returning(RiskDecisionRow.id)
        )
        if await self._session.scalar(stmt) is None:
            msg = f"intent {decision.intent_id} already has a risk decision"
            raise ConflictingRecordError(msg)

    async def get_for_intent(self, intent_id: UUID) -> RiskDecision | None:
        row = await self._session.scalar(
            select(RiskDecisionRow).where(RiskDecisionRow.intent_id == intent_id)
        )
        return risk_decision_from_row(row) if row is not None else None


def _check_event(event: OrderEvent, order: Order, *, from_status: OrderStatus | None) -> None:
    if (
        event.order_id != order.id
        or event.from_status is not from_status
        or event.to_status is not order.status
    ):
        msg = (
            f"event ({event.order_id}: {event.from_status} -> {event.to_status}) does not "
            f"describe order {order.id}: {from_status} -> {order.status}"
        )
        raise ValueError(msg)
