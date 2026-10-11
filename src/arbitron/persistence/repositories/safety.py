from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from arbitron.domain.risk import KILL_SWITCH_TRANSITIONS, KillSwitch
from arbitron.persistence.mapping import kill_switch_from_row
from arbitron.persistence.tables import KillSwitchEventRow, KillSwitchRow
from arbitron.ports.repositories import ConcurrencyConflict

GLOBAL_SCOPE = "global"


class SqlKillSwitchRepository:
    def __init__(self, session: AsyncSession, scope: str = GLOBAL_SCOPE) -> None:
        self._session = session
        self._scope = scope

    async def get(self) -> KillSwitch | None:
        row = await self._session.get(KillSwitchRow, self._scope, populate_existing=True)
        return kill_switch_from_row(row) if row is not None else None

    async def save(self, previous: KillSwitch | None, updated: KillSwitch) -> None:
        values = {
            "state": updated.state.value,
            "changed_at": updated.changed_at,
            "actor": updated.actor,
            "reason": updated.reason,
        }
        if previous is None:
            stmt = (
                insert(KillSwitchRow)
                .values(scope=self._scope, **values)
                .on_conflict_do_nothing(index_elements=["scope"])
                .returning(KillSwitchRow.scope)
            )
        else:
            if updated.state not in KILL_SWITCH_TRANSITIONS[previous.state]:
                msg = f"kill switch: {previous.state} -> {updated.state} is not allowed"
                raise ValueError(msg)
            stmt = (
                update(KillSwitchRow)
                .where(
                    KillSwitchRow.scope == self._scope,
                    KillSwitchRow.state == previous.state.value,
                    KillSwitchRow.changed_at == previous.changed_at,
                )
                .values(**values)
                .returning(KillSwitchRow.scope)
            )
        if await self._session.scalar(stmt) is None:
            msg = f"kill switch {self._scope!r} changed since it was read"
            raise ConcurrencyConflict(msg)
        await self._session.execute(
            insert(KillSwitchEventRow).values(
                scope=self._scope,
                from_state=previous.state.value if previous else None,
                to_state=updated.state.value,
                changed_at=updated.changed_at,
                actor=updated.actor,
                reason=updated.reason,
            )
        )
