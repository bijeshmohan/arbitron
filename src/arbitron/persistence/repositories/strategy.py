from datetime import datetime

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from arbitron.domain.market import Timeframe
from arbitron.domain.strategies import StrategyConfig, StrategyCursor
from arbitron.persistence.mapping import strategy_config_from_row, strategy_cursor_from_row
from arbitron.persistence.tables import StrategyConfigRow, StrategyCursorRow
from arbitron.ports.repositories import ConcurrencyConflict, ConflictingRecordError


class SqlStrategyConfigRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, config: StrategyConfig, *, created_at: datetime) -> bool:
        stmt = (
            insert(StrategyConfigRow)
            .values(
                config_hash=config.config_hash,
                strategy_id=config.strategy_id,
                version=config.version,
                parameters=dict(config.parameters),
                created_at=created_at,
            )
            .on_conflict_do_nothing(index_elements=["config_hash"])
            .returning(StrategyConfigRow.config_hash)
        )
        if await self._session.scalar(stmt) is not None:
            return True
        if await self.get(config.config_hash) != config:  # pragma: no cover - hash collision
            msg = f"strategy config {config.config_hash} exists with different content"
            raise ConflictingRecordError(msg)
        return False

    async def get(self, config_hash: str) -> StrategyConfig | None:
        row = await self._session.get(StrategyConfigRow, config_hash)
        return strategy_config_from_row(row) if row is not None else None


class SqlStrategyCursorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self, config_hash: str, exchange_id: str, symbol: str, timeframe: Timeframe
    ) -> StrategyCursor | None:
        row = await self._session.get(
            StrategyCursorRow,
            (config_hash, exchange_id, symbol, timeframe.value),
            populate_existing=True,
        )
        return strategy_cursor_from_row(row) if row is not None else None

    async def save(self, previous: StrategyCursor | None, updated: StrategyCursor) -> None:
        if previous is None:
            stmt = (
                insert(StrategyCursorRow)
                .values(
                    config_hash=updated.config_hash,
                    exchange_id=updated.exchange_id,
                    symbol=updated.symbol,
                    timeframe=updated.timeframe.value,
                    last_open_time=updated.last_open_time,
                    updated_at=updated.updated_at,
                )
                .on_conflict_do_nothing()
                .returning(StrategyCursorRow.config_hash)
            )
        else:
            identity = (previous.config_hash, previous.exchange_id, previous.symbol)
            if identity != (updated.config_hash, updated.exchange_id, updated.symbol) or (
                previous.timeframe != updated.timeframe
            ):
                msg = "a cursor update cannot change which strategy/market it tracks"
                raise ValueError(msg)
            if updated.last_open_time <= previous.last_open_time:
                msg = (
                    f"cursor cannot move backwards or stay put "
                    f"({previous.last_open_time.isoformat()} -> "
                    f"{updated.last_open_time.isoformat()})"
                )
                raise ValueError(msg)
            stmt = (
                update(StrategyCursorRow)
                .where(
                    StrategyCursorRow.config_hash == previous.config_hash,
                    StrategyCursorRow.exchange_id == previous.exchange_id,
                    StrategyCursorRow.symbol == previous.symbol,
                    StrategyCursorRow.timeframe == previous.timeframe.value,
                    StrategyCursorRow.last_open_time == previous.last_open_time,
                )
                .values(last_open_time=updated.last_open_time, updated_at=updated.updated_at)
                .returning(StrategyCursorRow.config_hash)
            )
        if await self._session.scalar(stmt) is None:
            msg = f"strategy cursor for {updated.symbol} changed since it was read"
            raise ConcurrencyConflict(msg)
