"""Durable kill switch operations (plan §11.3).

- First initialization puts the switch in ``RECOVERY_REVIEW``, not ``ARMED``:
  trading starts only after a named operator arms it.
- ``trip`` works from any state and is the emergency stop.
- ``arm`` requires ``RECOVERY_REVIEW`` and a named operator with a written
  acknowledgement; there is no automatic path back to trading.

Every change is persisted with its history row and a system event.
"""

import uuid
from collections.abc import Callable

from arbitron.domain.enums import KillSwitchState, Severity
from arbitron.domain.events import SystemEvent
from arbitron.domain.risk import SYSTEM_ACTOR, KillSwitch
from arbitron.ports.clock import Clock
from arbitron.ports.repositories import UnitOfWork

COMPONENT = "risk.kill_switch"
INITIAL_REASON = "initialized; awaiting operator review before first arming"


class KillSwitchService:
    def __init__(self, uow_factory: Callable[[], UnitOfWork], clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def status(self) -> KillSwitch | None:
        async with self._uow_factory() as tx:
            return await tx.kill_switch.get()

    async def ensure_initialized(self) -> KillSwitch:
        async with self._uow_factory() as tx:
            current = await tx.kill_switch.get()
            if current is not None:
                return current
            initial = KillSwitch(
                state=KillSwitchState.RECOVERY_REVIEW,
                changed_at=self._clock.now(),
                actor=SYSTEM_ACTOR,
                reason=INITIAL_REASON,
            )
            await tx.kill_switch.save(None, initial)
            await tx.system_events.append(self._event("initialized", Severity.WARNING, initial))
            await tx.commit()
            return initial

    async def trip(self, *, reason: str, actor: str = SYSTEM_ACTOR) -> KillSwitch:
        return await self._change(
            "tripped",
            Severity.CRITICAL,
            lambda ks: ks.trip(at=self._clock.now(), reason=reason, actor=actor),
        )

    async def begin_review(self, *, reason: str, actor: str = SYSTEM_ACTOR) -> KillSwitch:
        return await self._change(
            "review_started",
            Severity.WARNING,
            lambda ks: ks.begin_review(at=self._clock.now(), reason=reason, actor=actor),
        )

    async def arm(self, *, operator: str, acknowledgement: str) -> KillSwitch:
        return await self._change(
            "armed",
            Severity.WARNING,
            lambda ks: ks.rearm(
                at=self._clock.now(), operator=operator, acknowledgement=acknowledgement
            ),
        )

    async def _change(
        self, kind: str, severity: Severity, transition: Callable[[KillSwitch], KillSwitch]
    ) -> KillSwitch:
        await self.ensure_initialized()
        async with self._uow_factory() as tx:
            current = await tx.kill_switch.get()
            if current is None:  # pragma: no cover - ensured above
                msg = "kill switch disappeared after initialization"
                raise RuntimeError(msg)
            updated = transition(current)
            await tx.kill_switch.save(current, updated)
            await tx.system_events.append(self._event(kind, severity, updated, previous=current))
            await tx.commit()
            return updated

    def _event(
        self,
        kind: str,
        severity: Severity,
        state: KillSwitch,
        *,
        previous: KillSwitch | None = None,
    ) -> SystemEvent:
        return SystemEvent(
            event_type=f"kill_switch.{kind}",
            severity=severity,
            component=COMPONENT,
            occurred_at=state.changed_at,
            correlation_id=f"kill-switch-{uuid.uuid4()}",
            payload={
                "from_state": previous.state.value if previous else None,
                "to_state": state.state.value,
                "actor": state.actor,
                "reason": state.reason,
            },
        )
