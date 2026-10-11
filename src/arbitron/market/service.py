"""Wires adapter, collector, and monitor together for one config (used by the CLI)."""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from arbitron.config.settings import ArbitronConfig
from arbitron.domain.market import Timeframe
from arbitron.market.candles import align_down
from arbitron.market.collector import CandleCollector, SyncResult
from arbitron.market.freshness import DataQualityReport, MarketDataMonitor, QualityThresholds
from arbitron.ports.clock import Clock
from arbitron.ports.errors import VenueError
from arbitron.ports.market_data import MarketDataPort
from arbitron.ports.repositories import UnitOfWork


@dataclass(frozen=True, slots=True)
class CycleOutcome:
    results: tuple[SyncResult, ...]
    reports: tuple[DataQualityReport, ...]

    @property
    def ok(self) -> bool:
        return all(report.ok for report in self.reports)


class MarketDataService:
    def __init__(
        self,
        config: ArbitronConfig,
        *,
        market_data: MarketDataPort,
        uow_factory: Callable[[], UnitOfWork],
        clock: Clock,
        lookback: int,
    ) -> None:
        self._config = config
        self._market_data = market_data
        self._clock = clock
        self._lookback = lookback
        self.timeframe = Timeframe(config.strategy.timeframe)
        self._metadata_refresh = timedelta(seconds=config.market.metadata_max_age_seconds) / 2
        self._last_refresh: datetime | None = None
        self.collector = CandleCollector(
            market_data=market_data,
            uow_factory=uow_factory,
            clock=clock,
            exchange_id=config.exchange.id,
            timeframe=self.timeframe,
            backfill_candles=config.market.backfill_candles,
            max_pages=config.market.max_pages_per_sync,
        )
        self.monitor = MarketDataMonitor(
            exchange_id=config.exchange.id,
            timeframe=self.timeframe,
            uow_factory=uow_factory,
            clock=clock,
            thresholds=QualityThresholds(
                max_candle_age=timedelta(seconds=config.risk.max_market_data_age_seconds),
                metadata_max_age=timedelta(seconds=config.market.metadata_max_age_seconds),
                max_clock_skew=timedelta(milliseconds=config.market.max_clock_skew_ms),
            ),
        )

    @property
    def symbols(self) -> Sequence[str]:
        return self._config.exchange.symbols

    async def run_cycle(self) -> CycleOutcome:
        """Refresh metadata if due, sync every symbol, then evaluate the quality gate."""
        now = self._clock.now()
        if self._last_refresh is None or now - self._last_refresh >= self._metadata_refresh:
            await self.monitor.refresh(self._market_data)
            self._last_refresh = now
        results = [await self.collector.sync(symbol) for symbol in self.symbols]
        reports = [
            await self.monitor.evaluate(symbol, lookback=self._lookback) for symbol in self.symbols
        ]
        return CycleOutcome(tuple(results), tuple(reports))

    def seconds_until_next_close(self) -> float:
        """Time until the next candle closes plus the close grace period."""
        now = self._clock.now()
        next_close = align_down(now, self.timeframe) + self.timeframe.duration
        wake = next_close + timedelta(seconds=self._config.market.close_grace_seconds)
        return max((wake - now).total_seconds(), 0.0)

    async def follow(
        self,
        on_cycle: Callable[[CycleOutcome], None],
        on_error: Callable[[VenueError], None],
        *,
        stop: asyncio.Event,
    ) -> None:
        """Run a cycle after every candle close until ``stop`` is set.

        A venue failure (after read retries) is reported and the next cycle is
        attempted. Meanwhile the stored data ages and the quality gate turns
        ``STALE``, so a persistent outage fails closed without stopping ingestion.
        """
        while not stop.is_set():
            try:
                on_cycle(await self.run_cycle())
            except VenueError as exc:
                on_error(exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.seconds_until_next_close())
            except TimeoutError:
                continue
