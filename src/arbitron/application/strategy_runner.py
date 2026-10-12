"""Live strategy evaluation over stored, finalized candles (plan §10.2).

Per symbol, ``process``:

1. refuses to evaluate unless the market-data quality report is ``ok``;
2. on first run, evaluates only the latest closed candle (history is never
   back-filled into signals; use ``replay`` for that);
3. otherwise evaluates every closed candle after the cursor, in order, skipping
   (and recording) candles whose lookback window is incomplete;
4. persists the config snapshot, new signals, and the advanced cursor in one
   transaction, so a crash either records all of it or none of it.

Signals are deduplicated by identity, so re-processing after a crash is
harmless. Signals for older candles (from catching up after downtime) are
persisted for the audit trail; deciding whether a signal is too old to act on
belongs to the order pipeline (Phase 5/6).
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from arbitron.domain.enums import Severity
from arbitron.domain.events import SystemEvent
from arbitron.domain.market import Candle, Timeframe
from arbitron.domain.portfolio import PortfolioView
from arbitron.domain.signals import Signal
from arbitron.domain.strategies import StrategyCursor
from arbitron.market.freshness import DataQualityReport
from arbitron.ports.clock import Clock
from arbitron.ports.repositories import UnitOfWork
from arbitron.strategy.base import Strategy, StrategyError
from arbitron.strategy.evaluation import config_of, evaluate_candle, window_problem

COMPONENT = "strategy.runner"


@dataclass(frozen=True, slots=True)
class RunResult:
    symbol: str
    evaluated: tuple[datetime, ...]  # open times of candles evaluated
    signals: tuple[Signal, ...]  # signals newly persisted by this run
    skipped: tuple[str, ...]  # why candles or the whole run were skipped
    cursor: datetime | None


@dataclass
class _Batch:
    """Mutable working state of one ``process`` call."""

    symbol: str
    correlation_id: str
    now: datetime  # from the injected clock; never wall-clock here
    evaluated: list[datetime] = field(default_factory=list[datetime])
    signals: list[Signal] = field(default_factory=list[Signal])
    skipped: list[str] = field(default_factory=list[str])
    events: list[SystemEvent] = field(default_factory=list[SystemEvent])


class StrategyRunner:
    def __init__(
        self,
        strategy: Strategy,
        *,
        exchange_id: str,
        timeframe: Timeframe,
        uow_factory: Callable[[], UnitOfWork],
        clock: Clock,
        portfolio: Callable[[], PortfolioView] | None = None,
        max_catch_up: int = 500,
    ) -> None:
        self._strategy = strategy
        self._config = config_of(strategy)
        self._exchange_id = exchange_id
        self._timeframe = timeframe
        self._uow_factory = uow_factory
        self._clock = clock
        # Phase 6 supplies the live portfolio projection; until then strategies
        # see an empty, read-only view.
        self._portfolio = portfolio or (lambda: PortfolioView(as_of=clock.now()))
        self._max_catch_up = max_catch_up

    @property
    def config_hash(self) -> str:
        return self._config.config_hash

    async def process(self, symbol: str, quality: DataQualityReport) -> RunResult:
        if quality.symbol != symbol:
            msg = f"quality report is for {quality.symbol}, not {symbol}"
            raise ValueError(msg)
        batch = _Batch(symbol, f"strategy-{uuid.uuid4()}", now=self._clock.now())
        if not quality.ok:
            codes = ", ".join(sorted({i.code.value for i in quality.issues if i.blocking}))
            self._skip(batch, "blocked", f"market data quality gate is blocking: {codes}")
            await self._record(batch.events)
            return RunResult(symbol, (), (), tuple(batch.skipped), None)

        loaded = await self._load(symbol)
        if isinstance(loaded, RunResult):
            return loaded
        cursor, latest, candles, first_target = loaded

        targets = [c for c in candles if c.open_time >= first_target]
        if len(targets) > self._max_catch_up:
            dropped = len(targets) - self._max_catch_up
            self._skip(
                batch,
                "catch_up_truncated",
                f"{dropped} candles behind the catch-up limit were not evaluated "
                f"(from {targets[0].open_time.isoformat()})",
            )
            targets = targets[-self._max_catch_up :]

        await self._evaluate(targets, {c.open_time: c for c in candles}, batch)
        persisted = await self._persist(cursor, latest, batch)
        return RunResult(
            symbol, tuple(batch.evaluated), tuple(persisted), tuple(batch.skipped), latest.open_time
        )

    async def _load(
        self, symbol: str
    ) -> tuple[StrategyCursor | None, Candle, Sequence[Candle], datetime] | RunResult:
        """Cursor, latest closed candle, candles to evaluate (with lookback), first target."""
        duration = self._timeframe.duration
        async with self._uow_factory() as tx:
            cursor = await tx.strategy_cursors.get(
                self._config.config_hash, self._exchange_id, symbol, self._timeframe
            )
            latest = await tx.candles.latest_closed(self._exchange_id, symbol, self._timeframe)
            if latest is None or (cursor is not None and latest.open_time <= cursor.last_open_time):
                return RunResult(symbol, (), (), (), _cursor_time(cursor))
            first_target = latest.open_time if cursor is None else cursor.last_open_time + duration
            candles = await tx.candles.closed_range(
                self._exchange_id,
                symbol,
                self._timeframe,
                start=first_target - (self._strategy.lookback - 1) * duration,
                end=latest.open_time + duration,
            )
        return cursor, latest, candles, first_target

    async def _evaluate(
        self, targets: Sequence[Candle], by_time: dict[datetime, Candle], batch: _Batch
    ) -> None:
        lookback = self._strategy.lookback
        portfolio = self._portfolio()
        for target in targets:
            window = _window(by_time, target.open_time, lookback, self._timeframe)
            problem = window_problem(window, self._timeframe, lookback)
            if problem is not None:
                self._skip(batch, "window_incomplete", f"{target.open_time.isoformat()}: {problem}")
                continue
            try:
                signal = evaluate_candle(
                    self._strategy,
                    window=window,
                    timeframe=self._timeframe,
                    portfolio=portfolio,
                    created_at=batch.now,
                )
            except StrategyError as exc:
                cause = f"{exc}: {exc.__cause__!r}" if exc.__cause__ else str(exc)
                batch.events.append(self._event("failed", Severity.CRITICAL, batch, cause))
                await self._record(batch.events)
                raise
            batch.evaluated.append(target.open_time)
            if signal is not None:
                batch.signals.append(signal)

    async def _persist(
        self, cursor: StrategyCursor | None, latest: Candle, batch: _Batch
    ) -> list[Signal]:
        persisted: list[Signal] = []
        async with self._uow_factory() as tx:
            await tx.strategy_configs.add_if_absent(self._config, created_at=batch.now)
            for signal in batch.signals:
                if await tx.signals.add_if_absent(signal):
                    persisted.append(signal)
            await tx.strategy_cursors.save(
                cursor,
                StrategyCursor(
                    config_hash=self._config.config_hash,
                    exchange_id=self._exchange_id,
                    symbol=batch.symbol,
                    timeframe=self._timeframe,
                    last_open_time=latest.open_time,
                    updated_at=batch.now,
                ),
            )
            for event in batch.events:
                await tx.system_events.append(event)
            await tx.commit()
        return persisted

    def _skip(self, batch: _Batch, kind: str, reason: str) -> None:
        batch.skipped.append(reason)
        batch.events.append(self._event(kind, Severity.WARNING, batch, reason))

    def _event(self, kind: str, severity: Severity, batch: _Batch, detail: str) -> SystemEvent:
        return SystemEvent(
            event_type=f"strategy.{kind}",
            severity=severity,
            component=COMPONENT,
            occurred_at=self._clock.now(),
            correlation_id=batch.correlation_id,
            payload={
                "strategy_id": self._config.strategy_id,
                "strategy_version": self._config.version,
                "config_hash": self._config.config_hash,
                "exchange_id": self._exchange_id,
                "symbol": batch.symbol,
                "timeframe": self._timeframe.value,
                "detail": detail,
            },
        )

    async def _record(self, events: Sequence[SystemEvent]) -> None:
        async with self._uow_factory() as tx:
            for event in events:
                await tx.system_events.append(event)
            await tx.commit()


def _window(
    by_time: dict[datetime, Candle], end: datetime, lookback: int, timeframe: Timeframe
) -> list[Candle]:
    """Candles at the ``lookback`` expected open times ending at ``end``; missing ones omitted."""
    times = [end - (lookback - 1 - i) * timeframe.duration for i in range(lookback)]
    return [by_time[t] for t in times if t in by_time]


def _cursor_time(cursor: StrategyCursor | None) -> datetime | None:
    return cursor.last_open_time if cursor is not None else None
