from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from arbitron.domain.events import SystemEvent
from arbitron.persistence.mapping import system_event_from_row, system_event_values
from arbitron.persistence.tables import SystemEventRow


class SqlSystemEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: SystemEvent) -> None:
        await self._session.execute(insert(SystemEventRow).values(**system_event_values(event)))

    async def list_recent(self, limit: int) -> Sequence[SystemEvent]:
        rows = await self._session.scalars(
            select(SystemEventRow).order_by(SystemEventRow.id.desc()).limit(limit)
        )
        return [system_event_from_row(row) for row in rows]
