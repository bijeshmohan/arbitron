from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from arbitron.domain.enums import Severity
from arbitron.domain.events import SystemEvent
from arbitron.persistence.mapping import system_event_from_row, system_event_values
from arbitron.persistence.tables import HeartbeatRow, SystemEventRow


class SqlSystemEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: SystemEvent) -> None:
        await self._session.execute(insert(SystemEventRow).values(**system_event_values(event)))

    async def latest_id(self) -> int:
        return int(await self._session.scalar(select(func.max(SystemEventRow.id))) or 0)

    async def list_after(
        self, after_id: int, severities: frozenset[Severity], limit: int
    ) -> Sequence[tuple[int, SystemEvent]]:
        rows = await self._session.scalars(
            select(SystemEventRow)
            .where(
                SystemEventRow.id > after_id,
                SystemEventRow.severity.in_([s.value for s in severities]),
            )
            .order_by(SystemEventRow.id)
            .limit(limit)
        )
        return [(row.id, system_event_from_row(row)) for row in rows]

    async def list_recent(self, limit: int) -> Sequence[SystemEvent]:
        rows = await self._session.scalars(
            select(SystemEventRow).order_by(SystemEventRow.id.desc()).limit(limit)
        )
        return [system_event_from_row(row) for row in rows]


class SqlHeartbeatRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self, *, component: str, instance: str, beat_at: datetime, status: str, detail: str
    ) -> None:
        stmt = insert(HeartbeatRow).values(
            component=component, instance=instance, last_beat=beat_at, status=status, detail=detail
        )
        await self._session.execute(
            stmt.on_conflict_do_update(
                index_elements=["component", "instance"],
                set_={
                    "last_beat": stmt.excluded.last_beat,
                    "status": stmt.excluded.status,
                    "detail": stmt.excluded.detail,
                },
            )
        )
