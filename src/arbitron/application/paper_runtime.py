"""The unattended paper-trading process: session + supervisor + monitoring (plan §15).

Supervised tasks:

| task           | critical | does                                                   |
|----------------|----------|--------------------------------------------------------|
| trading        | yes      | one session cycle after every candle close             |
| reconciliation | yes      | ``execution.reconciliation_interval_seconds``          |
| lock-watch     | yes      | verifies the single-instance lock every 15 s (fatal)  |
| alerts         | no       | sends critical events to the operator channel          |
| heartbeats     | no       | persists task health to the ``heartbeats`` table       |
| http           | no       | ``/health``, ``/ready``, ``/metrics``                  |

Startup recovery (``PaperSession.start``) completes before any task runs.
Alerts start from *before* startup, so startup discrepancies are alerted too.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import timedelta

from arbitron.application.paper_session import PaperSession, SessionOutcome
from arbitron.application.supervisor import (
    FatalTaskError,
    Supervisor,
    SupervisorOutcome,
    TaskContext,
    TaskSpec,
)
from arbitron.config.settings import ArbitronConfig
from arbitron.domain.enums import KillSwitchState, Severity
from arbitron.domain.money import canonical_str
from arbitron.execution.order_manager import Action
from arbitron.execution.reconciler import ReconciliationReport
from arbitron.monitoring.alerts import AlertDispatcher
from arbitron.monitoring.health import HealthRegistry, Readiness
from arbitron.monitoring.http import create_app, serve
from arbitron.monitoring.logging import Redactor
from arbitron.monitoring.metrics import Metrics
from arbitron.persistence.lock import InstanceLock, LockLostError
from arbitron.ports.clock import Clock
from arbitron.ports.errors import VenueError
from arbitron.ports.notifier import Notifier
from arbitron.ports.repositories import UnitOfWork

logger = logging.getLogger("arbitron.runtime")
LOCK_CHECK_SECONDS = 15
HEARTBEAT_PERSIST_SECONDS = 30
ALERT_POLL_SECONDS = 5


class PaperRuntime:
    def __init__(
        self,
        config: ArbitronConfig,
        *,
        session: PaperSession,
        notifier: Notifier,
        uow_factory: Callable[[], UnitOfWork],
        clock: Clock,
        lock: InstanceLock | None,
        metrics: Metrics | None = None,
        redactor: Redactor | None = None,
        on_cycle: Callable[[SessionOutcome], None] | None = None,
        serve_http: bool = True,
        restart_backoff: timedelta = timedelta(seconds=5),
    ) -> None:
        self._config = config
        self.session = session
        self._uow_factory = uow_factory
        self._clock = clock
        self._lock = lock
        self.metrics = metrics or Metrics()
        self._on_cycle = on_cycle
        self._serve_http = serve_http
        self.health = HealthRegistry(clock)
        self.metrics.watch_heartbeats(self.health.ages)
        self.dispatcher = AlertDispatcher(
            notifier=notifier,
            uow_factory=uow_factory,
            clock=clock,
            min_severity=Severity(config.alerts.min_severity),
            dedupe_window=timedelta(minutes=config.alerts.dedupe_minutes),
            max_per_hour=config.alerts.max_per_hour,
            redactor=redactor,
            on_outcome=lambda outcome: self.metrics.alerts.labels(outcome).inc(),
        )
        self.supervisor = Supervisor(
            health=self.health,
            kill_switch=session.kill_switch,
            uow_factory=uow_factory,
            clock=clock,
            max_restarts=config.monitoring.task_max_restarts,
            window=timedelta(seconds=config.monitoring.task_restart_window_seconds),
            backoff=restart_backoff,
            on_restart=lambda task: self.metrics.task_restarts.labels(task).inc(),
        )
        self._last_outcome: SessionOutcome | None = None

    async def run(self, stop: asyncio.Event) -> SupervisorOutcome:
        await self.dispatcher.start_from_now()
        startup = await self.session.start()
        self._observe_reconciliation(startup.reconciliation)
        logger.info(
            "startup complete",
            extra={
                "funded": startup.funded,
                "recovered_orders": startup.recovered,
                "reconciliation_clean": startup.reconciliation.clean,
            },
        )
        outcome = await self.supervisor.run(self._tasks(), stop)
        try:  # best effort: deliver alerts raised during shutdown (e.g. restart limit)
            async with asyncio.timeout(10):
                await self.dispatcher.poll()
        except Exception:
            logger.exception("final alert delivery failed")
        return outcome

    def _tasks(self) -> list[TaskSpec]:
        timeframe = self.session.timeframe.duration
        interval = timedelta(seconds=self._config.execution.reconciliation_interval_seconds)
        specs = [
            TaskSpec(
                "trading",
                self._trading,
                critical=True,
                stale_after=timeframe * 2 + timedelta(minutes=2),
            ),
            TaskSpec(
                "reconciliation",
                self._reconciliation,
                critical=True,
                stale_after=interval * 3 + timedelta(minutes=1),
            ),
            TaskSpec("alerts", self._alerts, critical=False, stale_after=timedelta(minutes=2)),
            TaskSpec(
                "heartbeats", self._heartbeats, critical=False, stale_after=timedelta(minutes=2)
            ),
        ]
        if self._lock is not None:
            specs.append(
                TaskSpec(
                    "lock-watch", self._lock_watch, critical=True, stale_after=timedelta(minutes=1)
                )
            )
        if self._serve_http:
            specs.append(
                TaskSpec("http", self._http, critical=False, stale_after=timedelta(days=365))
            )
        return specs

    # --- tasks ------------------------------------------------------------------

    async def _trading(self, ctx: TaskContext) -> None:
        while not ctx.stop.is_set():
            started = time.monotonic()
            try:
                outcome = await self.session.run_cycle()
            except VenueError as exc:
                # Not a crash: data ages, the quality gate blocks, reconciliation
                # stays pending. docs/failure-matrix.md #16.
                logger.warning("cycle skipped: venue error", extra={"error": str(exc)})
                ctx.beat("venue error; data will go stale if this persists")
            else:
                self.metrics.cycle_seconds.observe(time.monotonic() - started)
                await self._observe_cycle(outcome)
                if self._on_cycle is not None:
                    self._on_cycle(outcome)
                ctx.beat(f"cycle ok, {len(outcome.orders)} order action(s)")
            await ctx.sleep(self.session.market.seconds_until_next_close())

    async def _reconciliation(self, ctx: TaskContext) -> None:
        interval = self._config.execution.reconciliation_interval_seconds
        ctx.beat("startup reconciliation done")
        while not ctx.stop.is_set():
            await ctx.sleep(interval)
            if ctx.stop.is_set():
                return
            report = await self.session.reconcile_now()
            self._observe_reconciliation(report)
            ctx.beat("clean" if report.clean else "not clean")

    async def _lock_watch(self, ctx: TaskContext) -> None:
        assert self._lock is not None  # noqa: S101 - only scheduled with a lock
        while not ctx.stop.is_set():
            try:
                await self._lock.check()
            except LockLostError as exc:
                raise FatalTaskError(str(exc)) from exc
            ctx.beat()
            await ctx.sleep(LOCK_CHECK_SECONDS)

    async def _alerts(self, ctx: TaskContext) -> None:
        await self.dispatcher.run(ctx.stop, interval=ALERT_POLL_SECONDS, beat=ctx.beat)

    async def _heartbeats(self, ctx: TaskContext) -> None:
        instance = self._config.paper.account
        while not ctx.stop.is_set():
            async with self._uow_factory() as tx:
                for name, view in self.health.view().items():
                    await tx.heartbeats.upsert(
                        component=name,
                        instance=instance,
                        beat_at=self._clock.now(),
                        status=str(view["status"]),
                        detail=str(view["detail"]),
                    )
                await tx.commit()
            ctx.beat()
            await ctx.sleep(HEARTBEAT_PERSIST_SECONDS)

    async def _http(self, ctx: TaskContext) -> None:
        ctx.beat("serving")
        app = create_app(health=self.health, readiness=self.readiness, metrics=self.metrics)
        await serve(
            app,
            host=self._config.monitoring.http_host,
            port=self._config.monitoring.http_port,
            stop=ctx.stop,
        )

    # --- readiness and metrics -----------------------------------------------------

    async def readiness(self) -> Readiness:
        checks: dict[str, str] = {
            "startup": "ok" if self.session.ready else "startup recovery not complete"
        }
        try:
            state = await self.session.kill_switch.status()
            checks["kill_switch"] = (
                "ok"
                if state is not None and state.state is KillSwitchState.ARMED
                else f"kill switch {state.state.value if state else 'uninitialized'}"
            )
        except Exception as exc:
            checks["database"] = f"unreachable ({type(exc).__name__})"
        report = self.session.last_reconciliation
        checks["reconciliation"] = (
            "ok"
            if report is not None and report.clean
            else "not clean"
            if report is not None
            else "not run yet"
        )
        if self._last_outcome is not None:
            failing = [r.symbol for r in self._last_outcome.pipeline.market.reports if not r.ok]
            checks["market_data"] = (
                "ok" if not failing else f"quality gate blocking: {', '.join(failing)}"
            )
        checks.update(self.health.task_checks())
        return Readiness(checks)

    async def _observe_cycle(self, outcome: SessionOutcome) -> None:
        self._last_outcome = outcome
        now = self._clock.now()
        timeframe = self.session.timeframe.duration
        for result in outcome.pipeline.market.results:
            if result.latest_closed is not None:
                age = now - (result.latest_closed + timeframe)
                self.metrics.market_data_age.labels(result.symbol).set(age.total_seconds())
        for report in outcome.pipeline.market.reports:
            self.metrics.quality_gate_ok.labels(report.symbol).set(1 if report.ok else 0)
        for run in outcome.pipeline.runs:
            for signal in run.signals:
                self.metrics.signals.labels(signal.side.value).inc()
        for order in outcome.orders:
            self.metrics.order_outcomes.labels(order.action.value).inc()
            if order.action is Action.RISK_REJECTED:
                for reason in filter(None, order.detail.split(",")):
                    self.metrics.risk_rejections.labels(reason).inc()
        self.metrics.set_equity(outcome.equity, self._config.exchange.quote_currency)
        if outcome.reconciliation is not None:
            self._observe_reconciliation(outcome.reconciliation)
        try:
            state = await self.session.kill_switch.status()
            self.metrics.set_kill_switch(state.state if state else None)
        except Exception:
            logger.warning("kill switch state unavailable for metrics", exc_info=True)
        logger.info(
            "cycle complete",
            extra={
                "equity": canonical_str(outcome.equity) if outcome.equity is not None else None,
                "orders": len(outcome.orders),
                "signals": sum(len(r.signals) for r in outcome.pipeline.runs),
                "market_data_ok": outcome.pipeline.market.ok,
                "tripped": ",".join(r.value for r in outcome.tripped) or None,
            },
        )

    def _observe_reconciliation(self, report: ReconciliationReport) -> None:
        self.metrics.reconciliation_runs.labels("clean" if report.clean else "not_clean").inc()
        self.metrics.reconciliation_discrepancies.set(len(report.discrepancies))
        self.metrics.reconciliation_pending.set(len(report.pending))
        if report.discrepancies:
            logger.error("reconciliation discrepancies", extra={"count": len(report.discrepancies)})
