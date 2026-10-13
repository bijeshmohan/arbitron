"""Task supervision (plan §15.2; operator decision 2026-10-13).

Each named task runs under a runner that records heartbeats and handles
failure:

- A crash (any exception, or a long-running task returning unexpectedly) is
  recorded as a critical ``supervisor.task_crashed`` event (which becomes an
  alert) and the task restarts after exponential backoff.
- More than ``max_restarts`` crashes within ``window``:
  - **critical** task: ``supervisor.restart_limit`` event, kill switch
    tripped, whole session stopped;
  - non-critical task: marked failed and left stopped (the session runs on;
    ``/health`` shows it).
- ``FatalTaskError`` (e.g. the instance lock was lost) skips restarts: trip
  and stop at once, since restarting cannot fix it.

Shutdown (``stop`` set, SIGTERM): runners are cancelled and awaited with a
timeout. Cancellation reaching an in-flight order submission is recorded as
``SUBMISSION_UNKNOWN`` by the order manager, so it is reconciled on restart.
"""

import asyncio
import logging
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from arbitron.application.kill_switch import KillSwitchService
from arbitron.domain.enums import Severity
from arbitron.domain.events import PayloadValue, SystemEvent
from arbitron.monitoring.health import HealthRegistry, TaskStatus
from arbitron.ports.clock import Clock
from arbitron.ports.repositories import UnitOfWork

logger = logging.getLogger("arbitron.supervisor")


class FatalTaskError(Exception):
    """A failure no restart can fix; stop the session immediately."""


@dataclass(frozen=True, slots=True)
class TaskContext:
    name: str
    stop: asyncio.Event
    report: Callable[[str], None]

    def beat(self, detail: str = "") -> None:
        """Report progress; a task that stops beating becomes not ready."""
        self.report(detail)

    async def sleep(self, seconds: float) -> None:
        """Sleep, waking early when the session stops."""
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=max(seconds, 0))
        except TimeoutError:
            return


@dataclass(frozen=True, slots=True)
class TaskSpec:
    name: str
    run: Callable[[TaskContext], Awaitable[None]]
    critical: bool
    stale_after: timedelta  # no heartbeat for longer than this => not ready


@dataclass(frozen=True, slots=True)
class SupervisorOutcome:
    failure: str | None  # None when stopped on request

    @property
    def clean(self) -> bool:
        return self.failure is None


class Supervisor:
    def __init__(
        self,
        *,
        health: HealthRegistry,
        kill_switch: KillSwitchService,
        uow_factory: Callable[[], UnitOfWork],
        clock: Clock,
        max_restarts: int,
        window: timedelta,
        backoff: timedelta = timedelta(seconds=5),
        max_backoff: timedelta = timedelta(minutes=1),
        shutdown_timeout: timedelta = timedelta(seconds=20),
        on_restart: Callable[[str], None] | None = None,
    ) -> None:
        self._health = health
        self._kill_switch = kill_switch
        self._uow_factory = uow_factory
        self._clock = clock
        self._max_restarts = max_restarts
        self._window = window
        self._backoff = backoff
        self._max_backoff = max_backoff
        self._shutdown_timeout = shutdown_timeout
        self._on_restart = on_restart
        self._failure: str | None = None

    async def run(self, specs: list[TaskSpec], stop: asyncio.Event) -> SupervisorOutcome:
        for spec in specs:
            self._health.register(spec.name, critical=spec.critical, stale_after=spec.stale_after)
        runners = [asyncio.create_task(self._runner(spec, stop), name=spec.name) for spec in specs]
        logger.info("supervisor started", extra={"tasks": ",".join(s.name for s in specs)})
        try:
            await stop.wait()
        finally:
            for runner in runners:
                runner.cancel()
            done, pending = await asyncio.wait(
                runners, timeout=self._shutdown_timeout.total_seconds()
            )
            for task in pending:
                logger.error("task did not stop in time", extra={"task": task.get_name()})
            for spec in specs:
                self._health.set_status(spec.name, TaskStatus.STOPPED)
            logger.info(
                "supervisor stopped",
                extra={"failure": self._failure, "stopped": len(done), "stuck": len(pending)},
            )
        return SupervisorOutcome(self._failure)

    async def _runner(self, spec: TaskSpec, stop: asyncio.Event) -> None:
        crashes: deque[datetime] = deque()
        context = TaskContext(
            spec.name, stop, lambda detail="": self._health.beat(spec.name, detail)
        )
        while not stop.is_set():
            self._health.set_status(spec.name, TaskStatus.RUNNING)
            logger.info("task starting", extra={"task": spec.name})
            try:
                await spec.run(context)
                if stop.is_set():
                    return
                msg = "task returned while the session was still running"
                raise RuntimeError(msg)  # handled below, exactly like a crash
            except asyncio.CancelledError:
                raise
            except FatalTaskError as exc:
                await self._give_up(spec, stop, f"fatal: {exc}", fatal=True)
                return
            except Exception as exc:
                now = self._clock.now()
                crashes.append(now)
                while crashes and now - crashes[0] > self._window:
                    crashes.popleft()
                error = f"{type(exc).__name__}: {exc}"
                logger.error("task crashed", exc_info=exc, extra={"task": spec.name})
                if len(crashes) > self._max_restarts:
                    await self._give_up(spec, stop, error, fatal=False)
                    return
                await self._record(
                    "task_crashed",
                    task=spec.name,
                    error=error[:500],
                    restart=f"{len(crashes)} of {self._max_restarts}",
                )
                self._health.set_status(spec.name, TaskStatus.RESTARTING, error[:200])
                if self._on_restart is not None:
                    self._on_restart(spec.name)
                delay = min(self._backoff * 2 ** (len(crashes) - 1), self._max_backoff)
                await context.sleep(delay.total_seconds())

    async def _give_up(
        self, spec: TaskSpec, stop: asyncio.Event, error: str, *, fatal: bool
    ) -> None:
        # Record durably first, then change visible state: anything reacting to
        # FAILED (including a shutdown) must find the event already written.
        if not spec.critical and not fatal:
            await self._record("task_failed", task=spec.name, error=error[:500])
            self._health.set_status(spec.name, TaskStatus.FAILED, error[:200])
            return
        self._health.set_status(spec.name, TaskStatus.FAILED, error[:200])
        reason = (
            f"task {spec.name} failed: {error}"
            if fatal
            else (
                f"task {spec.name} crashed more than {self._max_restarts} times "
                f"in {self._window}: {error}"
            )
        )
        self._failure = reason
        await self._record("restart_limit", task=spec.name, error=error[:500])
        try:
            await self._kill_switch.trip(reason=reason[:500])
        except Exception:  # the database may be the very thing that failed
            logger.critical("could not trip the kill switch", exc_info=True)
        logger.critical("stopping session", extra={"task": spec.name, "reason": reason})
        stop.set()

    async def _record(self, kind: str, **payload: PayloadValue) -> None:
        try:
            async with self._uow_factory() as tx:
                await tx.system_events.append(
                    SystemEvent(
                        event_type=f"supervisor.{kind}",
                        severity=Severity.CRITICAL,
                        component="supervisor",
                        occurred_at=self._clock.now(),
                        correlation_id=f"supervisor-{uuid.uuid4()}",
                        payload=payload,
                    )
                )
                await tx.commit()
        except Exception:  # never let event recording take the supervisor down
            logger.critical("could not record supervisor event", exc_info=True, extra=payload)
