"""A paper-trading session: the full loop, one cycle per candle close.

Per cycle:

1. sync market data and run the quality gate; evaluate the strategy;
2. reconcile with the venue if due (and straight after any ambiguous
   submission), see ``docs/failure-matrix.md``;
3. build the trading snapshot (records equity; unresolved reconciliation
   blocks new orders);
4. trip the kill switch on a daily-loss or drawdown breach;
5. for each symbol, act on its latest signal if it is fresh (latest closed
   candle, within one interval) and has not been acted on already.

The caller must hold the single-instance lock for the paper account.
"""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from arbitron.application.kill_switch import KillSwitchService
from arbitron.application.pipeline import PipelineOutcome, SignalPipeline
from arbitron.application.policies import risk_policy, sizing_policy
from arbitron.application.snapshot_builder import SnapshotBuilder
from arbitron.application.strategy_runner import StrategyRunner
from arbitron.config.settings import ArbitronConfig
from arbitron.domain.enums import Severity
from arbitron.domain.events import SystemEvent
from arbitron.domain.market import Timeframe
from arbitron.domain.risk import RiskReason
from arbitron.domain.snapshot import TradingSnapshot
from arbitron.execution.freshness import stale_reason
from arbitron.execution.order_manager import Action, OrderManager, OrderOutcome
from arbitron.execution.reconciler import Reconciler, ReconciliationReport
from arbitron.market.freshness import DataQualityReport
from arbitron.market.service import MarketDataService, follow_closes
from arbitron.paper.broker import PaperBroker
from arbitron.ports.clock import Clock
from arbitron.ports.errors import VenueError
from arbitron.ports.execution import ExecutionPort
from arbitron.ports.market_data import MarketDataPort
from arbitron.ports.repositories import UnitOfWork
from arbitron.risk.exposure import ValuationUnavailableError, valuation
from arbitron.risk.manager import loss_limit_breaches
from arbitron.simulation.fill_model import FillModel
from arbitron.strategy.base import Strategy

TRIP_REASONS = frozenset({RiskReason.MAX_DAILY_LOSS, RiskReason.MAX_DRAWDOWN})


@dataclass(frozen=True, slots=True)
class StartupReport:
    funded: bool  # the paper account was created and funded now
    recovered: int  # orders found mid-submission and marked unknown
    reconciliation: ReconciliationReport


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    pipeline: PipelineOutcome
    equity: Decimal | None
    tripped: tuple[RiskReason, ...]
    orders: tuple[OrderOutcome, ...]
    skipped_signals: tuple[str, ...]
    reconciliation: ReconciliationReport | None = None  # if one ran this cycle


class PaperSession:
    def __init__(
        self,
        config: ArbitronConfig,
        *,
        strategy: Strategy,
        market_data: MarketDataPort,
        session_factory: async_sessionmaker[AsyncSession],
        uow_factory: Callable[[], UnitOfWork],
        clock: Clock,
        fill_model: FillModel | None = None,
        submission_port: Callable[[PaperBroker], ExecutionPort] | None = None,
    ) -> None:
        """``submission_port`` wraps the venue for order *submission* only; used to
        inject faults (lost responses, timeouts). Reconciliation always reads the
        venue directly, as it would read a real exchange."""
        self._config = config
        self._clock = clock
        self._uow_factory = uow_factory
        self.timeframe = Timeframe(config.strategy.timeframe)
        self._risk_policy = risk_policy(config)
        self.market = MarketDataService(
            config,
            market_data=market_data,
            uow_factory=uow_factory,
            clock=clock,
            lookback=strategy.lookback,
        )
        self.runner = StrategyRunner(
            strategy,
            exchange_id=config.exchange.id,
            timeframe=self.timeframe,
            uow_factory=uow_factory,
            clock=clock,
        )
        self._pipeline = SignalPipeline(
            self.market, self.runner, strategy_enabled=config.strategy.enabled
        )
        self.kill_switch = KillSwitchService(uow_factory, clock)
        self.broker = PaperBroker(
            session_factory=session_factory,
            account=config.paper.account,
            exchange_id=config.exchange.id,
            quote_currency=config.exchange.quote_currency,
            market_data=market_data,
            markets=lambda: self.market.monitor.markets,
            fill_model=fill_model
            or FillModel(
                slippage_bps=config.paper.slippage_bps,
                fee_rate=config.paper.fee_pct / Decimal(100),
            ),
            clock=clock,
        )
        broker = self.broker
        self._snapshots = SnapshotBuilder(
            exchange_id=config.exchange.id,
            quote_currency=config.exchange.quote_currency,
            symbols=config.exchange.symbols,
            timeframe=self.timeframe,
            market_data=market_data,
            account=broker,
            markets=lambda: self.market.monitor.markets,
            uow_factory=uow_factory,
            clock=clock,
            trading_enabled=config.risk.trading_enabled,
        )
        self._orders = OrderManager(
            execution=submission_port(broker) if submission_port else broker,
            uow_factory=uow_factory,
            clock=clock,
            sizing_policy=sizing_policy(config),
            risk_policy=self._risk_policy,
            submission_timeout=timedelta(seconds=config.execution.submission_timeout_seconds),
            on_authentication_failure=lambda reason: self.kill_switch.trip(reason=reason),
        )
        self.reconciler = Reconciler(
            execution=broker,
            account=broker,
            order_manager=self._orders,
            kill_switch=self.kill_switch,
            uow_factory=uow_factory,
            clock=clock,
            markets=lambda: self.market.monitor.markets,
            exchange_id=config.exchange.id,
            symbols=config.exchange.symbols,
            settle=timedelta(seconds=config.execution.unknown_order_settle_seconds),
            max_age=timedelta(seconds=config.execution.unknown_order_max_age_seconds),
        )
        self._market_data = market_data
        self._reconcile_every = timedelta(seconds=config.execution.reconciliation_interval_seconds)
        self._last_reconciliation: ReconciliationReport | None = None
        self._ready = False
        # Trading cycles and reconciliation never interleave: reconciliation
        # must not observe a submission between the venue's commit and ours.
        self._mutex = asyncio.Lock()

    async def start(self) -> StartupReport:
        """Startup recovery sequence (plan §14.1). Trading is enabled only afterwards.

        Kill switch initialized; paper account funded once; market metadata
        loaded; orders interrupted mid-submission marked unknown; then a full
        reconciliation. Discrepancies move the kill switch to review.
        """
        await self.kill_switch.ensure_initialized()
        funded = await self.broker.ensure_funded(self._config.paper.starting_balance)
        if funded:
            await self._event(
                "paper.funded",
                Severity.INFO,
                account=self.broker.account,
                amount=str(self._config.paper.starting_balance),
                asset=self._config.exchange.quote_currency,
            )
        await self.market.monitor.refresh(self._market_data)
        recovered = await self.reconciler.recover_interrupted()
        report = await self._reconcile()
        self._ready = True
        return StartupReport(funded=funded, recovered=len(recovered), reconciliation=report)

    async def _reconcile(self) -> ReconciliationReport:
        report = await self.reconciler.run()
        self._last_reconciliation = report
        return report

    def _reconciliation_due(self) -> bool:
        last = self._last_reconciliation
        return last is None or self._clock.now() - last.started_at >= self._reconcile_every

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def last_reconciliation(self) -> ReconciliationReport | None:
        return self._last_reconciliation

    async def reconcile_now(self) -> ReconciliationReport:
        """Periodic reconciliation, serialized with trading cycles."""
        async with self._mutex:
            return await self._reconcile()

    async def run_cycle(self) -> SessionOutcome:
        async with self._mutex:
            return await self._run_cycle()

    async def _run_cycle(self) -> SessionOutcome:
        if not self._ready:
            msg = "call start() before running cycles"
            raise RuntimeError(msg)
        pipeline = await self._pipeline.run_cycle()
        reports = {r.symbol: r for r in pipeline.market.reports}
        reconciled = await self._reconcile() if self._reconciliation_due() else None
        snapshot = await self._snapshot(reports)
        equity, tripped = await self._enforce_loss_limits(snapshot)
        if tripped:
            snapshot = await self._snapshot(reports)

        latest = {r.symbol: r.latest_closed for r in pipeline.market.results}
        outcomes: list[OrderOutcome] = []
        skipped: list[str] = []
        if self._config.strategy.enabled:
            for symbol in self._config.exchange.symbols:
                async with self._uow_factory() as tx:
                    signal = await tx.signals.latest_for(
                        self.runner.config_hash, self._config.exchange.id, symbol, self.timeframe
                    )
                if signal is None:
                    continue
                reason = stale_reason(
                    signal,
                    latest_closed_open=latest.get(symbol),
                    now=self._clock.now(),
                    timeframe=self.timeframe,
                )
                if reason is not None:
                    skipped.append(f"{symbol}: {reason}")
                    continue
                outcome = await self._orders.act_on_signal(signal, snapshot)
                outcomes.append(outcome)
                if outcome.action is Action.SUBMISSION_UNKNOWN:
                    # docs/failure-matrix.md: reconcile straight after ambiguity.
                    reconciled = await self._reconcile()
                if outcome.order is not None and (
                    outcome.order.exchange_order_id or outcome.action is Action.SUBMISSION_UNKNOWN
                ):
                    snapshot = await self._snapshot(reports)
        return SessionOutcome(
            pipeline, equity, tripped, tuple(outcomes), tuple(skipped), reconciled
        )

    async def _snapshot(self, reports: Mapping[str, DataQualityReport]) -> TradingSnapshot:
        last = self._last_reconciliation
        return await self._snapshots.build(
            reports,
            system_ready=self._ready,
            reconciliation_clean=last is not None and last.clean,
        )

    async def follow(
        self,
        on_cycle: Callable[[SessionOutcome], None],
        on_error: Callable[[VenueError], None],
        *,
        stop: asyncio.Event,
    ) -> None:
        await follow_closes(
            self.run_cycle, on_cycle, on_error, stop=stop, wait=self.market.seconds_until_next_close
        )

    async def _enforce_loss_limits(
        self, snapshot: TradingSnapshot
    ) -> tuple[Decimal | None, tuple[RiskReason, ...]]:
        try:
            equity = valuation(snapshot).equity
        except ValuationUnavailableError:
            return None, ()
        breaches = tuple(
            r for r in loss_limit_breaches(snapshot, self._risk_policy, equity) if r in TRIP_REASONS
        )
        if (
            not breaches
            or snapshot.kill_switch is None
            or not snapshot.kill_switch.allows_new_orders
        ):
            return equity, ()
        await self.kill_switch.trip(
            reason=f"automatic: {', '.join(r.value for r in breaches)} (equity {equity})"
        )
        return equity, breaches

    async def _event(self, kind: str, severity: Severity, **payload: str) -> None:
        now: datetime = self._clock.now()
        async with self._uow_factory() as tx:
            await tx.system_events.append(
                SystemEvent(
                    kind, severity, "paper.session", now, f"{kind}-{now.isoformat()}", payload
                )
            )
            await tx.commit()
