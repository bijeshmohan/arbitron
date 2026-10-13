"""Running and recording backtests and parameter sweeps (plan §17.1).

Reads markets and candles only from the database (fill it with
``arbitron market backfill``), runs the pure engine, computes metrics, and
records an immutable ``ExperimentRecord`` with everything needed to
reproduce the run: data range, a fingerprint of every candle read, strategy
config, risk policy version, cost assumptions, and code version.
"""

import asyncio
import hashlib
import itertools
import os
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from arbitron import __version__
from arbitron.application.policies import risk_policy, risk_policy_version, sizing_policy
from arbitron.backtest import engine, metrics
from arbitron.config.settings import ArbitronConfig, Mode
from arbitron.domain.experiments import ExperimentRecord
from arbitron.domain.market import Candle, Market, Timeframe
from arbitron.domain.money import canonical_str
from arbitron.domain.strategies import ParameterValue
from arbitron.ports.clock import Clock
from arbitron.ports.repositories import UnitOfWork
from arbitron.strategy.evaluation import config_of
from arbitron.strategy.registry import build_strategy

_HUNDRED = Decimal(100)


class BacktestDataError(Exception):
    """The database lacks the markets or candles the backtest needs."""


def code_version() -> str:
    """Package version plus the git commit (``.dirty`` if modified).

    Containers have no git checkout: the image records the commit at build
    time in ``ARBITRON_BUILD_REVISION``.
    """
    revision = os.environ.get("ARBITRON_BUILD_REVISION")
    if revision and revision != "unknown":
        return f"{__version__}+g{revision}"
    root = Path(__file__).resolve().parents[3]
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],  # noqa: S607 - git on PATH
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],  # noqa: S607
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return __version__
    return f"{__version__}+g{sha}{'.dirty' if dirty else ''}"


def fingerprint(candles: Mapping[str, Sequence[Candle]]) -> str:
    digest = hashlib.sha256()
    for symbol in sorted(candles):
        for c in candles[symbol]:
            fields = (
                symbol,
                c.timeframe.value,
                c.open_time.isoformat(),
                c.open,
                c.high,
                c.low,
                c.close,
                c.volume,
            )
            digest.update(
                "|".join(canonical_str(f) if isinstance(f, Decimal) else f for f in fields).encode()
            )
            digest.update(b"\n")
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class SweepRow:
    parameters: str
    in_sample: ExperimentRecord | None
    out_of_sample: ExperimentRecord | None
    error: str | None = None  # e.g. an invalid parameter combination


@dataclass(frozen=True, slots=True)
class Run:
    record: ExperimentRecord
    result: engine.Result


class BacktestService:
    def __init__(
        self,
        config: ArbitronConfig,
        *,
        uow_factory: Callable[[], UnitOfWork],
        clock: Clock,
        version: str | None = None,
    ) -> None:
        self._config = config
        self._uow_factory = uow_factory
        self._clock = clock
        self._version = version or code_version()
        self.timeframe = Timeframe(config.strategy.timeframe)

    async def run(
        self,
        *,
        start: datetime,
        end: datetime,
        parameters: Mapping[str, ParameterValue] | None = None,
        label: str = "run",
    ) -> Run:
        config = self._config
        params = {**config.strategy.parameters, **(parameters or {})}
        strategy = build_strategy(
            config.strategy.id, config.strategy.version, params, live=config.app.mode is Mode.LIVE
        )
        markets, candles = await self._load(start, end, warmup=strategy.lookback)
        bt = config.backtest
        assumptions = engine.Assumptions(
            starting_balance=bt.starting_balance,
            spread_bps=bt.spread_bps,
            slippage_bps=bt.slippage_bps,
            fee_rate=bt.fee_pct / _HUNDRED,
        )
        inputs = engine.Inputs(
            strategy=strategy,
            sizing=sizing_policy(config),
            risk=risk_policy(config),
            markets=markets,
            candles=candles,
            timeframe=self.timeframe,
            quote_currency=config.exchange.quote_currency,
            trading_enabled=config.risk.trading_enabled,
            start=start,
        )
        # CPU-bound and pure: keep it off the event loop.
        result = await asyncio.to_thread(engine.run, inputs, assumptions)
        record = ExperimentRecord(
            id=uuid.uuid4(),
            created_at=self._clock.now(),
            label=label,
            exchange_id=config.exchange.id,
            symbols=tuple(config.exchange.symbols),
            timeframe=self.timeframe.value,
            start=start,
            end=end,
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.version,
            config_hash=config_of(strategy).config_hash,
            risk_policy_version=risk_policy_version(config),
            code_version=self._version,
            data_fingerprint=fingerprint(candles),
            candle_count=sum(len(cs) for cs in candles.values()),
            assumptions=assumptions.describe(),
            parameters=dict(strategy.parameters),
            metrics=metrics.compute(result, assumptions, timeframe=self.timeframe, candles=candles),
        )
        async with self._uow_factory() as tx:
            await tx.experiments.add(record)
            await tx.commit()
        return Run(record, result)

    async def sweep(
        self,
        *,
        start: datetime,
        split: datetime,
        end: datetime,
        grid: Mapping[str, Sequence[ParameterValue]],
    ) -> list[SweepRow]:
        """Every parameter combination on the in-sample and out-of-sample periods."""
        if not start < split < end:
            msg = "need start < split < end"
            raise ValueError(msg)
        sweep_id = uuid.uuid4().hex[:8]
        names = sorted(grid)
        rows: list[SweepRow] = []
        for values in itertools.product(*(grid[n] for n in names)):
            params: dict[str, ParameterValue] = dict(zip(names, values, strict=True))
            text = ", ".join(f"{k}={v}" for k, v in params.items())
            try:
                in_sample = await self.run(
                    start=start, end=split, parameters=params, label=f"sweep:{sweep_id}:in_sample"
                )
                out_sample = await self.run(
                    start=split, end=end, parameters=params, label=f"sweep:{sweep_id}:out_of_sample"
                )
            except ValueError as exc:  # e.g. fast >= slow: report and move on
                rows.append(SweepRow(text, None, None, str(exc)))
                continue
            rows.append(SweepRow(text, in_sample.record, out_sample.record))
        return rows

    async def _load(
        self, start: datetime, end: datetime, *, warmup: int
    ) -> tuple[dict[str, Market], dict[str, list[Candle]]]:
        if end <= start:
            msg = "backtest end must be after start"
            raise BacktestDataError(msg)
        exchange = self._config.exchange.id
        markets: dict[str, Market] = {}
        candles: dict[str, list[Candle]] = {}
        async with self._uow_factory() as tx:
            for symbol in self._config.exchange.symbols:
                market = await tx.markets.get(exchange, symbol)
                if market is None:
                    msg = (
                        f"no stored market rules for {symbol}; run `arbitron market backfill` first"
                    )
                    raise BacktestDataError(msg)
                markets[symbol] = market
                series = await tx.candles.closed_range(
                    exchange,
                    symbol,
                    self.timeframe,
                    start=start - warmup * self.timeframe.duration,
                    end=end,
                )
                if not any(c.open_time >= start for c in series):
                    msg = (
                        f"no stored candles for {symbol} [{self.timeframe}] between "
                        f"{start.isoformat()} and {end.isoformat()}; run `arbitron market backfill`"
                    )
                    raise BacktestDataError(msg)
                candles[symbol] = list(series)
        return markets, candles
