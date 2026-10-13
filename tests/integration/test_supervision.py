"""Supervisor, alert dispatch, and the paper runtime under failure (Phase 8 acceptance)."""

import asyncio
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from arbitron.application.kill_switch import KillSwitchService
from arbitron.application.paper_runtime import PaperRuntime
from arbitron.application.paper_session import PaperSession
from arbitron.application.supervisor import FatalTaskError, Supervisor, TaskContext, TaskSpec
from arbitron.clocks import ManualClock
from arbitron.domain.enums import KillSwitchState, Severity, Side
from arbitron.domain.events import Alert, SystemEvent
from arbitron.monitoring.alerts import AlertDispatcher, NotifierError
from arbitron.monitoring.health import HealthRegistry, TaskStatus
from arbitron.persistence.db import create_session_factory
from arbitron.persistence.lock import LockLostError
from tests.factories import T0
from tests.integration.conftest import UowFactory
from tests.integration.test_market_ingestion import NOW, FakeVenue, history
from tests.integration.test_paper_trading import ScriptedStrategy, config

FAST = timedelta(milliseconds=10)


class RecordingNotifier:
    def __init__(self, failures: int = 0) -> None:
        self.alerts: list[Alert] = []
        self.failures = failures

    async def notify(self, alert: Alert) -> None:
        if self.failures:
            self.failures -= 1
            raise NotifierError("channel down")
        self.alerts.append(alert)

    @property
    def titles(self) -> list[str]:
        return [a.title for a in self.alerts]


async def events(engine: AsyncEngine, prefix: str) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT event_type FROM system_events WHERE event_type LIKE :p ORDER BY id"),
            {"p": f"{prefix}%"},
        )
        return [r[0] for r in rows]


async def wait_for(condition: Callable[[], bool], seconds: float = 10) -> None:
    async with asyncio.timeout(seconds):
        while not condition():  # noqa: ASYNC110 - polling observable state is the point
            await asyncio.sleep(0.01)


def task_field(health: HealthRegistry, task: str, field: str) -> object:
    """A task's health field, or None if the supervisor has not registered it yet."""
    return health.view().get(task, {}).get(field)


# --- supervisor --------------------------------------------------------------------


def supervisor(
    uow: UowFactory, health: HealthRegistry, clock: ManualClock, **kw: Any
) -> Supervisor:
    return Supervisor(
        health=health,
        kill_switch=KillSwitchService(uow, clock),
        uow_factory=uow,
        clock=clock,
        max_restarts=kw.get("max_restarts", 3),
        window=timedelta(minutes=10),
        backoff=FAST,
        max_backoff=FAST,
        shutdown_timeout=timedelta(seconds=2),
    )


def forever(name: str, beats: list[str]) -> TaskSpec:
    async def run(ctx: TaskContext) -> None:
        while not ctx.stop.is_set():
            beats.append(name)
            ctx.beat()
            await ctx.sleep(0.01)

    return TaskSpec(name, run, critical=True, stale_after=timedelta(minutes=1))


class TestSupervisor:
    async def test_critical_task_crash_loop_trips_and_stops(
        self, uow: UowFactory, engine: AsyncEngine
    ) -> None:
        clock = ManualClock(T0)
        health = HealthRegistry(clock)
        attempts = 0

        async def crashing(ctx: TaskContext) -> None:
            nonlocal attempts
            attempts += 1
            raise RuntimeError("boom")

        outcome = await asyncio.wait_for(
            supervisor(uow, health, clock).run(
                [TaskSpec("trading", crashing, critical=True, stale_after=timedelta(minutes=1))],
                asyncio.Event(),
            ),
            timeout=10,
        )

        assert attempts == 4  # first run + 3 restarts
        assert outcome.failure is not None
        assert "more than 3 times" in outcome.failure
        state = await KillSwitchService(uow, clock).status()
        assert state is not None
        assert state.state is KillSwitchState.TRIPPED
        assert await events(engine, "supervisor.") == [
            "supervisor.task_crashed",
            "supervisor.task_crashed",
            "supervisor.task_crashed",
            "supervisor.restart_limit",
        ]
        assert health.view()["trading"]["status"] == TaskStatus.STOPPED.value

    async def test_crashes_outside_the_window_do_not_accumulate(self, uow: UowFactory) -> None:
        clock = ManualClock(T0)
        stop = asyncio.Event()
        attempts = 0

        async def occasionally_crashes(ctx: TaskContext) -> None:
            nonlocal attempts
            attempts += 1
            clock.advance(timedelta(minutes=11))  # each crash is in a fresh window
            if attempts >= 6:
                stop.set()
                return
            raise RuntimeError("transient")

        outcome = await asyncio.wait_for(
            supervisor(uow, HealthRegistry(clock), clock).run(
                [
                    TaskSpec(
                        "trading",
                        occasionally_crashes,
                        critical=True,
                        stale_after=timedelta(minutes=1),
                    )
                ],
                stop,
            ),
            timeout=10,
        )

        assert outcome.clean
        assert attempts == 6

    async def test_fatal_error_stops_without_restarts(self, uow: UowFactory) -> None:
        clock = ManualClock(T0)
        attempts = 0

        async def fatal(ctx: TaskContext) -> None:
            nonlocal attempts
            attempts += 1
            raise FatalTaskError("instance lock lost")

        outcome = await asyncio.wait_for(
            supervisor(uow, HealthRegistry(clock), clock).run(
                [TaskSpec("lock-watch", fatal, critical=True, stale_after=timedelta(minutes=1))],
                asyncio.Event(),
            ),
            timeout=10,
        )

        assert attempts == 1
        assert outcome.failure is not None
        assert "fatal: instance lock lost" in outcome.failure

    async def test_non_critical_failure_does_not_stop_the_session(
        self, uow: UowFactory, engine: AsyncEngine
    ) -> None:
        clock = ManualClock(T0)
        health = HealthRegistry(clock)
        stop = asyncio.Event()
        beats: list[str] = []

        async def flaky(ctx: TaskContext) -> None:
            raise RuntimeError("alerts channel broken")

        run = asyncio.create_task(
            supervisor(uow, health, clock).run(
                [
                    forever("trading", beats),
                    TaskSpec("alerts", flaky, critical=False, stale_after=timedelta(minutes=1)),
                ],
                stop,
            )
        )
        await wait_for(lambda: task_field(health, "alerts", "status") == TaskStatus.FAILED.value)
        await wait_for(lambda: len(beats) > 5)
        assert not run.done()
        stop.set()
        outcome = await run

        assert outcome.clean
        assert "supervisor.task_failed" in await events(engine, "supervisor.")
        state = await KillSwitchService(uow, clock).status()
        assert state is None  # never tripped

    async def test_unexpected_return_counts_as_a_crash(self, uow: UowFactory) -> None:
        clock = ManualClock(T0)

        async def returns(ctx: TaskContext) -> None:
            return

        outcome = await asyncio.wait_for(
            supervisor(uow, HealthRegistry(clock), clock, max_restarts=0).run(
                [TaskSpec("trading", returns, critical=True, stale_after=timedelta(minutes=1))],
                asyncio.Event(),
            ),
            timeout=10,
        )

        assert outcome.failure is not None
        assert "returned while the session was still running" in outcome.failure

    async def test_stop_cancels_tasks_cleanly(self, uow: UowFactory) -> None:
        clock = ManualClock(T0)
        health = HealthRegistry(clock)
        stop = asyncio.Event()
        beats: list[str] = []
        cancelled = asyncio.Event()

        async def long_running(ctx: TaskContext) -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        run = asyncio.create_task(
            supervisor(uow, health, clock).run(
                [
                    forever("trading", beats),
                    TaskSpec("http", long_running, critical=False, stale_after=timedelta(days=1)),
                ],
                stop,
            )
        )
        await wait_for(lambda: len(beats) > 2)
        stop.set()
        outcome = await asyncio.wait_for(run, timeout=5)

        assert outcome.clean
        assert cancelled.is_set()
        assert {v["status"] for v in health.view().values()} == {TaskStatus.STOPPED.value}


# --- alert dispatcher -------------------------------------------------------------------


async def record_event(
    uow: UowFactory, event_type: str, severity: Severity = Severity.CRITICAL, **payload: str
) -> None:
    async with uow() as tx:
        await tx.system_events.append(SystemEvent(event_type, severity, "test", T0, "c", payload))
        await tx.commit()


def dispatcher(
    uow: UowFactory, clock: ManualClock, notifier: RecordingNotifier, **kw: Any
) -> AlertDispatcher:
    return AlertDispatcher(
        notifier=notifier,
        uow_factory=uow,
        clock=clock,
        min_severity=kw.get("min_severity", Severity.CRITICAL),
        dedupe_window=timedelta(minutes=30),
        max_per_hour=kw.get("max_per_hour", 20),
    )


class TestAlertDispatcher:
    async def test_only_new_events_at_or_above_severity(self, uow: UowFactory) -> None:
        clock = ManualClock(T0)
        notifier = RecordingNotifier()
        await record_event(uow, "kill_switch.tripped")  # history: not re-sent
        alerts = dispatcher(uow, clock, notifier)
        await alerts.start_from_now()
        await record_event(uow, "market_data.gap", Severity.WARNING, symbol="BTC/USDT")
        await record_event(uow, "order.submission_unknown", order_id="o-1")

        assert await alerts.poll() == 1
        assert notifier.titles == ["Order outcome unknown: o-1"]

    async def test_warnings_included_when_configured(self, uow: UowFactory) -> None:
        notifier = RecordingNotifier()
        alerts = dispatcher(uow, ManualClock(T0), notifier, min_severity=Severity.WARNING)
        await alerts.start_from_now()
        await record_event(uow, "market_data.gap", Severity.WARNING, symbol="BTC/USDT")

        assert await alerts.poll() == 1

    async def test_duplicates_suppressed_then_reported(self, uow: UowFactory) -> None:
        clock = ManualClock(T0)
        notifier = RecordingNotifier()
        alerts = dispatcher(uow, clock, notifier)
        await alerts.start_from_now()
        for _ in range(3):
            await record_event(uow, "supervisor.task_crashed", task="trading")
        await alerts.poll()
        assert len(notifier.alerts) == 1

        clock.advance(timedelta(minutes=31))
        await record_event(uow, "supervisor.task_crashed", task="trading")
        await alerts.poll()

        assert len(notifier.alerts) == 2
        assert "repeated 2 more time(s)" in notifier.alerts[1].message

    async def test_rate_limit_summarizes_suppressed(self, uow: UowFactory) -> None:
        clock = ManualClock(T0)
        notifier = RecordingNotifier()
        alerts = dispatcher(uow, clock, notifier, max_per_hour=2)
        await alerts.start_from_now()
        for i in range(5):
            await record_event(uow, "order.submission_unknown", order_id=f"o-{i}")
        await alerts.poll()
        assert len(notifier.alerts) == 2

        clock.advance(timedelta(hours=1))
        await record_event(uow, "order.submission_unknown", order_id="o-new")
        await alerts.poll()

        assert "3 alert(s) were suppressed by the rate limit" in notifier.alerts[-1].message

    async def test_delivery_failure_is_retried(self, uow: UowFactory) -> None:
        clock = ManualClock(T0)
        notifier = RecordingNotifier(failures=1)
        alerts = dispatcher(uow, clock, notifier)
        await alerts.start_from_now()
        await record_event(uow, "kill_switch.tripped")

        assert await alerts.poll() == 0
        clock.advance(timedelta(minutes=1))
        assert await alerts.poll() == 1
        assert notifier.titles == ["Kill switch TRIPPED"]

    async def test_undeliverable_alert_is_dropped_after_an_hour(self, uow: UowFactory) -> None:
        clock = ManualClock(T0)
        notifier = RecordingNotifier(failures=1000)
        alerts = dispatcher(uow, clock, notifier)
        await alerts.start_from_now()
        await record_event(uow, "kill_switch.tripped")
        await alerts.poll()

        for _ in range(12):
            clock.advance(timedelta(minutes=10))
            await alerts.poll()

        assert alerts.stats.dropped == 1
        assert notifier.alerts == []


# --- runtime (Phase 8 acceptance) --------------------------------------------------------


class FakeLock:
    def __init__(self) -> None:
        self.lost = False

    async def check(self) -> None:
        if self.lost:
            raise LockLostError("instance lock connection failed")


async def runtime_for(
    engine: AsyncEngine,
    uow: UowFactory,
    venue: FakeVenue,
    notifier: RecordingNotifier,
    *,
    lock: FakeLock | None = None,
) -> PaperRuntime:
    cfg = config()
    session = PaperSession(
        cfg,
        strategy=ScriptedStrategy({99: Side.BUY}),  # type: ignore[arg-type]
        market_data=venue,
        session_factory=create_session_factory(engine),
        uow_factory=uow,
        clock=venue.clock,
    )
    return PaperRuntime(
        cfg,
        session=session,
        notifier=notifier,
        uow_factory=uow,
        clock=venue.clock,
        lock=lock,  # type: ignore[arg-type]
        serve_http=False,
        restart_backoff=FAST,
    )


class TestPaperRuntime:
    async def test_crashing_trading_task_trips_kill_switch_and_alerts(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        venue = FakeVenue(ManualClock(NOW), history(200))
        notifier = RecordingNotifier()
        runtime = await runtime_for(engine, uow, venue, notifier)
        # A bug, not a venue error: every cycle crashes after startup.
        venue.fail_next = [RuntimeError("bug in cycle")] * 10

        outcome = await asyncio.wait_for(runtime.run(asyncio.Event()), timeout=20)

        assert outcome.failure is not None
        state = await runtime.session.kill_switch.status()
        assert state is not None
        assert state.state is KillSwitchState.TRIPPED
        titles = " | ".join(notifier.titles)
        assert "Task crashed and is restarting: trading" in titles
        assert "Task crashed repeatedly: session stopped: trading" in titles
        assert "Kill switch TRIPPED" in titles
        assert all("bug in cycle" in a.message for a in notifier.alerts if "crashed" in a.title)

    async def test_lost_lock_stops_the_session_immediately(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        venue = FakeVenue(ManualClock(NOW), history(200))
        notifier = RecordingNotifier()
        lock = FakeLock()
        runtime = await runtime_for(engine, uow, venue, notifier, lock=lock)
        lock.lost = True

        outcome = await asyncio.wait_for(runtime.run(asyncio.Event()), timeout=20)

        assert outcome.failure is not None
        assert "fatal" in outcome.failure
        assert any("Kill switch TRIPPED" in t for t in notifier.titles)

    async def test_healthy_session_is_ready_and_shuts_down_cleanly(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        venue = FakeVenue(ManualClock(NOW), history(200))
        notifier = RecordingNotifier()
        runtime = await runtime_for(engine, uow, venue, notifier, lock=FakeLock())
        await runtime.session.kill_switch.ensure_initialized()
        await runtime.session.kill_switch.arm(operator="tester", acknowledgement="ok")
        stop = asyncio.Event()

        run = asyncio.create_task(runtime.run(stop))
        await wait_for(
            lambda: task_field(runtime.health, "trading", "heartbeat_age_seconds") is not None
        )
        await wait_for(
            lambda: task_field(runtime.health, "heartbeats", "heartbeat_age_seconds") is not None
        )
        readiness = await runtime.readiness()
        metrics = runtime.metrics.render().decode()
        stop.set()
        outcome = await asyncio.wait_for(run, timeout=10)

        assert readiness.ready, readiness.checks
        assert outcome.clean
        assert 'arbitron_equity{quote="USDT"}' in metrics
        assert 'arbitron_order_outcomes_total{action="executed"} 1.0' in metrics
        assert notifier.alerts == []  # nothing went wrong
        async with engine.connect() as conn:
            persisted = await conn.scalar(text("SELECT count(*) FROM heartbeats"))
        assert persisted is not None
        assert persisted >= 4

    async def test_not_ready_while_kill_switch_not_armed(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        venue = FakeVenue(ManualClock(NOW), history(200))
        runtime = await runtime_for(engine, uow, venue, RecordingNotifier())
        await runtime.session.start()

        readiness = await runtime.readiness()

        assert not readiness.ready
        assert readiness.checks["kill_switch"] == "kill switch recovery_review"
