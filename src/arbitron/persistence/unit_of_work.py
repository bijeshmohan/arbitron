from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from arbitron.persistence.repositories import (
    SqlCandleRepository,
    SqlFillRepository,
    SqlKillSwitchRepository,
    SqlOrderRepository,
    SqlRiskDecisionRepository,
    SqlSignalRepository,
    SqlSystemEventRepository,
)
from arbitron.ports.repositories import (
    CandleRepository,
    FillRepository,
    KillSwitchRepository,
    OrderRepository,
    RiskDecisionRepository,
    SignalRepository,
    SystemEventRepository,
)


class SqlUnitOfWork:
    """One database transaction. Single use: ``async with SqlUnitOfWork(factory) as uow``.

    Nothing is durable until ``commit()``; leaving the block without committing
    (including via an exception) rolls back.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session = session_factory()
        self._entered = False
        self.candles: CandleRepository = SqlCandleRepository(self._session)
        self.signals: SignalRepository = SqlSignalRepository(self._session)
        self.orders: OrderRepository = SqlOrderRepository(self._session)
        self.fills: FillRepository = SqlFillRepository(self._session)
        self.risk_decisions: RiskDecisionRepository = SqlRiskDecisionRepository(self._session)
        self.kill_switch: KillSwitchRepository = SqlKillSwitchRepository(self._session)
        self.system_events: SystemEventRepository = SqlSystemEventRepository(self._session)

    async def __aenter__(self) -> Self:
        if self._entered:
            msg = "SqlUnitOfWork is single-use"
            raise RuntimeError(msg)
        self._entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            await self._session.rollback()
        finally:
            await self._session.close()

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()
