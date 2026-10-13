"""Operator actions exposed over the API (plan §16; operator decision 2026-10-13).

The API may only make the system *safer*: trip the kill switch and request
an immediate reconciliation. Reviewing, re-arming, and resolving orders stay
on the CLI, which needs shell access to the host.

Every action is audited: the kill switch records the operator as its actor
(``api:<operator>``) in its history and system events, and a reconciliation
request is recorded as a system event before it runs.
"""

import re
import uuid
from collections.abc import Awaitable, Callable

from arbitron.application.kill_switch import KillSwitchService
from arbitron.domain.enums import Severity
from arbitron.domain.events import SystemEvent
from arbitron.domain.risk import KillSwitch
from arbitron.execution.reconciler import ReconciliationReport
from arbitron.ports.clock import Clock
from arbitron.ports.repositories import UnitOfWork

COMPONENT = "api.operator"
OPERATOR_PATTERN = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
MAX_REASON_LENGTH = 500


def api_actor(operator: str) -> str:
    if not OPERATOR_PATTERN.match(operator):
        msg = "operator must be 1-64 characters: letters, digits, '_', '.', '@', '-'"
        raise ValueError(msg)
    return f"api:{operator}"


class OperatorActions:
    def __init__(
        self,
        *,
        kill_switch: KillSwitchService,
        reconcile: Callable[[], Awaitable[ReconciliationReport]],
        uow_factory: Callable[[], UnitOfWork],
        clock: Clock,
    ) -> None:
        self._kill_switch = kill_switch
        self._reconcile = reconcile
        self._uow_factory = uow_factory
        self._clock = clock

    async def trip_kill_switch(self, *, operator: str, reason: str) -> KillSwitch:
        actor = api_actor(operator)
        reason = reason.strip()
        if not reason or len(reason) > MAX_REASON_LENGTH:
            msg = f"reason must be 1-{MAX_REASON_LENGTH} characters"
            raise ValueError(msg)
        return await self._kill_switch.trip(reason=reason, actor=actor)

    async def request_reconciliation(self, *, operator: str) -> ReconciliationReport:
        actor = api_actor(operator)
        async with self._uow_factory() as tx:
            await tx.system_events.append(
                SystemEvent(
                    event_type="operator.reconciliation_requested",
                    severity=Severity.INFO,
                    component=COMPONENT,
                    occurred_at=self._clock.now(),
                    correlation_id=f"operator-{uuid.uuid4()}",
                    payload={"actor": actor},
                )
            )
            await tx.commit()
        return await self._reconcile()
