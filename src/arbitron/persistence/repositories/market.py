from collections.abc import Sequence
from datetime import datetime
from itertools import batched

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from arbitron.domain.market import Candle, Timeframe
from arbitron.persistence.mapping import candle_from_row, candle_values
from arbitron.persistence.tables import CandleRow

# 11 columns per row keeps each statement well under PostgreSQL's 65535-parameter limit.
_UPSERT_BATCH = 1000
_UPDATABLE = ("open", "high", "low", "close", "volume", "is_closed", "ingested_at")


class SqlCandleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, candles: Sequence[Candle]) -> int:
        written = 0
        for batch in batched(candles, _UPSERT_BATCH):
            stmt = insert(CandleRow).values([candle_values(c) for c in batch])
            stmt = stmt.on_conflict_do_update(
                index_elements=["exchange_id", "symbol", "timeframe", "open_time"],
                set_={column: stmt.excluded[column] for column in _UPDATABLE},
                # A stored closed candle is final.
                where=CandleRow.is_closed.is_(False),
            ).returning(CandleRow.open_time)
            result = await self._session.execute(stmt)
            written += len(result.all())
        return written

    async def latest_closed(
        self, exchange_id: str, symbol: str, timeframe: Timeframe
    ) -> Candle | None:
        stmt = (
            select(CandleRow)
            .where(
                CandleRow.exchange_id == exchange_id,
                CandleRow.symbol == symbol,
                CandleRow.timeframe == timeframe.value,
                CandleRow.is_closed.is_(True),
            )
            .order_by(CandleRow.open_time.desc())
            .limit(1)
        )
        row = await self._session.scalar(stmt)
        return candle_from_row(row) if row is not None else None

    async def closed_range(
        self,
        exchange_id: str,
        symbol: str,
        timeframe: Timeframe,
        *,
        start: datetime,
        end: datetime,
    ) -> Sequence[Candle]:
        stmt = (
            select(CandleRow)
            .where(
                CandleRow.exchange_id == exchange_id,
                CandleRow.symbol == symbol,
                CandleRow.timeframe == timeframe.value,
                CandleRow.is_closed.is_(True),
                CandleRow.open_time >= start,
                CandleRow.open_time < end,
            )
            .order_by(CandleRow.open_time)
        )
        rows = await self._session.scalars(stmt)
        return [candle_from_row(row) for row in rows]
