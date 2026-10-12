from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from arbitron.domain.portfolio import EquitySnapshot
from arbitron.domain.positions import Position
from arbitron.persistence.mapping import (
    equity_snapshot_from_row,
    equity_snapshot_values,
    position_from_row,
    position_values,
)
from arbitron.persistence.tables import EquitySnapshotRow, PositionRow
from arbitron.ports.repositories import ConcurrencyConflict


class SqlPositionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, exchange_id: str, symbol: str) -> Position | None:
        row = await self._session.get(PositionRow, (exchange_id, symbol), populate_existing=True)
        return position_from_row(row) if row is not None else None

    async def list(self, exchange_id: str) -> Sequence[Position]:
        rows = await self._session.scalars(
            select(PositionRow)
            .where(PositionRow.exchange_id == exchange_id)
            .order_by(PositionRow.symbol)
        )
        return [position_from_row(row) for row in rows]

    async def save(self, previous: Position | None, updated: Position) -> None:
        if previous is None:
            stmt = (
                insert(PositionRow)
                .values(**position_values(updated))
                .on_conflict_do_nothing()
                .returning(PositionRow.symbol)
            )
        else:
            if (previous.exchange_id, previous.symbol) != (updated.exchange_id, updated.symbol):
                msg = "a position update cannot change its market"
                raise ValueError(msg)
            stmt = (
                update(PositionRow)
                .where(
                    PositionRow.exchange_id == previous.exchange_id,
                    PositionRow.symbol == previous.symbol,
                    PositionRow.updated_at == previous.updated_at,
                    PositionRow.quantity == previous.quantity,
                )
                .values(**position_values(updated))
                .returning(PositionRow.symbol)
            )
        if await self._session.scalar(stmt) is None:
            msg = f"position {updated.symbol} changed since it was read"
            raise ConcurrencyConflict(msg)


class SqlEquitySnapshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, snapshot: EquitySnapshot) -> None:
        await self._session.execute(
            insert(EquitySnapshotRow).values(**equity_snapshot_values(snapshot))
        )

    async def first_since(self, quote_currency: str, since: datetime) -> EquitySnapshot | None:
        row = await self._session.scalar(
            select(EquitySnapshotRow)
            .where(
                EquitySnapshotRow.quote_currency == quote_currency,
                EquitySnapshotRow.taken_at >= since,
            )
            .order_by(EquitySnapshotRow.taken_at, EquitySnapshotRow.id)
            .limit(1)
        )
        return equity_snapshot_from_row(row) if row is not None else None

    async def peak(self, quote_currency: str) -> Decimal | None:
        return await self._session.scalar(
            select(func.max(EquitySnapshotRow.equity)).where(
                EquitySnapshotRow.quote_currency == quote_currency
            )
        )

    async def latest(self, quote_currency: str) -> EquitySnapshot | None:
        row = await self._session.scalar(
            select(EquitySnapshotRow)
            .where(EquitySnapshotRow.quote_currency == quote_currency)
            .order_by(EquitySnapshotRow.taken_at.desc(), EquitySnapshotRow.id.desc())
            .limit(1)
        )
        return equity_snapshot_from_row(row) if row is not None else None
