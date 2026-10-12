"""Market data -> quality gate -> strategy signals, one cycle per candle close."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from arbitron.application.strategy_runner import RunResult, StrategyRunner
from arbitron.market.service import CycleOutcome, MarketDataService, follow_closes
from arbitron.ports.errors import VenueError


@dataclass(frozen=True, slots=True)
class PipelineOutcome:
    market: CycleOutcome
    runs: tuple[RunResult, ...]  # empty when the strategy is disabled


class SignalPipeline:
    def __init__(
        self, market: MarketDataService, runner: StrategyRunner, *, strategy_enabled: bool
    ) -> None:
        self._market = market
        self._runner = runner
        self._strategy_enabled = strategy_enabled

    async def run_cycle(self) -> PipelineOutcome:
        market = await self._market.run_cycle()
        if not self._strategy_enabled:
            return PipelineOutcome(market, ())
        runs = [await self._runner.process(report.symbol, report) for report in market.reports]
        return PipelineOutcome(market, tuple(runs))

    async def follow(
        self,
        on_cycle: Callable[[PipelineOutcome], None],
        on_error: Callable[[VenueError], None],
        *,
        stop: asyncio.Event,
    ) -> None:
        await follow_closes(
            self.run_cycle,
            on_cycle,
            on_error,
            stop=stop,
            wait=self._market.seconds_until_next_close,
        )
