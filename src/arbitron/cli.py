"""Command-line entry point.

Commands are ``async`` and dispatched through a single ``asyncio.run`` so the
application is async from the first command onward.
"""

import argparse
import asyncio
import sys
import tomllib
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from arbitron import __version__
from arbitron.config import ArbitronConfig, Secrets, load_config

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncEngine

    from arbitron.application.kill_switch import KillSwitchService
    from arbitron.domain.risk import KillSwitch
    from arbitron.domain.signals import Signal
    from arbitron.exchange.ccxt_adapter import CcxtMarketData
    from arbitron.market.service import CycleOutcome
    from arbitron.ports.clock import Clock
    from arbitron.ports.errors import VenueError
    from arbitron.ports.repositories import UnitOfWork
    from arbitron.strategy.base import Strategy

EXIT_OK = 0
EXIT_INVALID_CONFIG = 1
EXIT_DATABASE_ERROR = 2
EXIT_DATA_QUALITY = 3
EXIT_VENUE_ERROR = 4
EXIT_INVALID_STATE = 5

Command = Callable[[argparse.Namespace], Coroutine[None, None, int]]


def _format_validation_error(exc: ValidationError) -> str:
    lines: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  {location}: {error['msg']}")
    return "\n".join(lines)


def _load_config(path: Path) -> ArbitronConfig | None:
    """Load and validate a config, reporting problems to stderr."""
    try:
        return load_config(path)
    except OSError as exc:
        print(f"error: cannot read {path}: {exc.strerror or exc}", file=sys.stderr)
    except tomllib.TOMLDecodeError as exc:
        print(f"error: {path} is not valid TOML: {exc}", file=sys.stderr)
    except ValidationError as exc:
        print(f"error: {path} is invalid:\n{_format_validation_error(exc)}", file=sys.stderr)
    return None


async def _check_config(args: argparse.Namespace) -> int:
    path: Path = args.config
    config = _load_config(path)
    if config is None:
        return EXIT_INVALID_CONFIG

    try:
        strategy = _require_strategy(config)
    except _CommandExit as exc:
        return exc.code
    from arbitron.application.policies import risk_policy_version  # noqa: PLC0415
    from arbitron.strategy.evaluation import config_of  # noqa: PLC0415

    secrets = Secrets()
    print(f"config OK: {path}")
    print(f"  mode:        {config.app.mode}")
    print(f"  exchange:    {config.exchange.id} (sandbox={config.exchange.sandbox})")
    print(f"  symbols:     {', '.join(config.exchange.symbols)}")
    print(
        f"  strategy:    {config.strategy.id} v{config.strategy.version} "
        f"[{config.strategy.timeframe}] lookback={strategy.lookback} "
        f"enabled={config.strategy.enabled}"
    )
    print(f"  strategy config hash: {config_of(strategy).config_hash}")
    print(
        f"  sizing:      {config.sizing.entry_notional} {config.exchange.quote_currency} "
        f"per entry, max slippage {config.sizing.max_slippage_pct}%"
    )
    print(f"  risk policy: {risk_policy_version(config)}")
    print(f"  credentials: {'present' if secrets.has_exchange_credentials else 'absent'}")
    print(f"  config hash: {config.config_hash()}")
    return EXIT_OK


def _database_url() -> str | None:
    url = Secrets().database_url
    if url is None:
        print("error: ARBITRON_DATABASE_URL is not set", file=sys.stderr)
        return None
    return url.get_secret_value()


def _database_failure(action: str, exc: Exception) -> int:
    # Never echo the URL: it contains the password.
    print(f"error: {action} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    return EXIT_DATABASE_ERROR


async def _db_upgrade(_args: argparse.Namespace) -> int:
    from alembic import command  # noqa: PLC0415 - keep CLI startup light

    from arbitron.persistence.db import alembic_config  # noqa: PLC0415

    url = _database_url()
    if url is None:
        return EXIT_DATABASE_ERROR
    try:
        # Alembic's async env runs its own event loop; keep it off this one.
        await asyncio.to_thread(command.upgrade, alembic_config(url), "head")
    except Exception as exc:
        return _database_failure("migration", exc)
    print("database schema upgraded to head")
    return EXIT_OK


async def _db_status(_args: argparse.Namespace) -> int:
    from arbitron.persistence.db import (  # noqa: PLC0415
        SchemaNotCurrentError,
        create_engine,
        verify_schema_current,
    )

    url = _database_url()
    if url is None:
        return EXIT_DATABASE_ERROR
    engine = create_engine(url, pool_size=1)
    try:
        revision = await verify_schema_current(engine)
    except SchemaNotCurrentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_DATABASE_ERROR
    except Exception as exc:
        return _database_failure("schema check", exc)
    finally:
        await engine.dispose()
    print(f"database schema is current (revision {revision})")
    return EXIT_OK


async def _schema_failure(engine: "AsyncEngine") -> int | None:
    """Exit code if the database is unreachable or not at the expected migration head."""
    from arbitron.persistence.db import SchemaNotCurrentError, verify_schema_current  # noqa: PLC0415

    try:
        await verify_schema_current(engine)
    except SchemaNotCurrentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_DATABASE_ERROR
    except Exception as exc:
        return _database_failure("schema check", exc)
    return None


class _CommandExit(Exception):  # noqa: N818 - control flow, not an error type
    """Stop a command with an exit code (the message has already been printed)."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def _require_config(path: Path) -> ArbitronConfig:
    config = _load_config(path)
    if config is None:
        raise _CommandExit(EXIT_INVALID_CONFIG)
    return config


def _require_strategy(config: ArbitronConfig) -> "Strategy":
    from arbitron.config.settings import Mode  # noqa: PLC0415
    from arbitron.strategy.registry import (  # noqa: PLC0415
        StrategyConfigError,
        UnknownStrategyError,
        build_strategy,
    )

    try:
        return build_strategy(
            config.strategy.id,
            config.strategy.version,
            config.strategy.parameters,
            live=config.app.mode is Mode.LIVE,
        )
    except (UnknownStrategyError, StrategyConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise _CommandExit(EXIT_INVALID_CONFIG) from exc


@dataclass
class _Runtime:
    clock: "Clock"
    uow_factory: "Callable[[], UnitOfWork]"
    market_data: "CcxtMarketData | None"


@asynccontextmanager
async def _runtime(config: ArbitronConfig, *, venue: bool) -> AsyncGenerator[_Runtime]:
    """Database (schema verified) and, if ``venue``, the market-data adapter; cleaned up after."""
    from arbitron.clocks import SystemClock  # noqa: PLC0415
    from arbitron.exchange.ccxt_adapter import create_market_data  # noqa: PLC0415
    from arbitron.persistence.db import create_engine, create_session_factory  # noqa: PLC0415
    from arbitron.persistence.unit_of_work import SqlUnitOfWork  # noqa: PLC0415

    url = _database_url()
    if url is None:
        raise _CommandExit(EXIT_DATABASE_ERROR)
    engine = create_engine(url)
    try:
        if (failure := await _schema_failure(engine)) is not None:
            raise _CommandExit(failure)
        clock = SystemClock()
        factory = create_session_factory(engine)
        market_data = create_market_data(config, clock) if venue else None
        try:
            yield _Runtime(clock, lambda: SqlUnitOfWork(factory), market_data)
        finally:
            if market_data is not None:
                await market_data.close()
    finally:
        await engine.dispose()


# Runs cycles until the event is set: (on_cycle, on_error, stop) -> None.
type _Follow[T] = Callable[
    [Callable[[T], None], Callable[[VenueError], None], asyncio.Event], Awaitable[None]
]


async def _run_cycles[T](
    *,
    follow: bool,
    once: "Callable[[], Awaitable[T]]",
    forever: "_Follow[T]",
    report: "Callable[[T], None]",
    ok: "Callable[[T], bool]",
) -> int:
    """One cycle (exit code reflects the outcome) or follow until SIGINT/SIGTERM."""
    import signal  # noqa: PLC0415

    from arbitron.ports.errors import VenueError  # noqa: PLC0415

    def report_error(exc: VenueError) -> None:
        print(f"error: venue request failed: {exc}", file=sys.stderr)

    try:
        if not follow:
            outcome = await once()
            report(outcome)
            return EXIT_OK if ok(outcome) else EXIT_DATA_QUALITY
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await forever(report, report_error, stop)
        return EXIT_OK
    except VenueError as exc:
        report_error(exc)
        return EXIT_VENUE_ERROR


def _print_market(outcome: "CycleOutcome", timeframe: str) -> None:
    for result, quality in zip(outcome.results, outcome.reports, strict=True):
        latest = result.latest_closed.isoformat() if result.latest_closed else "-"
        status = "OK" if quality.ok else "BLOCKED"
        print(
            f"{result.symbol} [{timeframe}] fetched={result.fetched} "
            f"written={result.written} latest_closed={latest} quality={status}"
        )
        for issue in (*result.issues, *quality.issues):
            marker = "!" if issue.blocking else "~"
            print(f"  {marker} {issue.code.value}: {issue.detail}")


def _print_signal(signal: "Signal") -> None:
    print(
        f"  signal {signal.side.value.upper()} {signal.symbol} "
        f"candle={signal.source_candle_time.isoformat()} reason={signal.reason_code}"
    )


async def _market_sync(args: argparse.Namespace) -> int:
    from arbitron.market.service import CycleOutcome, MarketDataService  # noqa: PLC0415

    config = _require_config(args.config)
    async with _runtime(config, venue=True) as rt:
        assert rt.market_data is not None  # noqa: S101 - venue=True
        service = MarketDataService(
            config,
            market_data=rt.market_data,
            uow_factory=rt.uow_factory,
            clock=rt.clock,
            lookback=args.lookback,
        )

        def report(outcome: CycleOutcome) -> None:
            _print_market(outcome, config.strategy.timeframe)
            sys.stdout.flush()

        return await _run_cycles(
            follow=args.follow,
            once=service.run_cycle,
            forever=lambda on_cycle, on_error, stop: service.follow(on_cycle, on_error, stop=stop),
            report=report,
            ok=lambda outcome: outcome.ok,
        )


async def _strategy_run(args: argparse.Namespace) -> int:
    from arbitron.application.pipeline import PipelineOutcome, SignalPipeline  # noqa: PLC0415
    from arbitron.application.strategy_runner import StrategyRunner  # noqa: PLC0415
    from arbitron.domain.market import Timeframe  # noqa: PLC0415
    from arbitron.market.service import MarketDataService  # noqa: PLC0415

    config = _require_config(args.config)
    strategy = _require_strategy(config)
    async with _runtime(config, venue=True) as rt:
        assert rt.market_data is not None  # noqa: S101 - venue=True
        market = MarketDataService(
            config,
            market_data=rt.market_data,
            uow_factory=rt.uow_factory,
            clock=rt.clock,
            lookback=strategy.lookback,
        )
        runner = StrategyRunner(
            strategy,
            exchange_id=config.exchange.id,
            timeframe=Timeframe(config.strategy.timeframe),
            uow_factory=rt.uow_factory,
            clock=rt.clock,
        )
        pipeline = SignalPipeline(market, runner, strategy_enabled=config.strategy.enabled)

        def report(outcome: PipelineOutcome) -> None:
            _print_market(outcome.market, config.strategy.timeframe)
            if not config.strategy.enabled:
                print("  strategy disabled in config: not evaluated")
            for run in outcome.runs:
                print(
                    f"  {strategy.strategy_id} v{strategy.version} evaluated="
                    f"{len(run.evaluated)} new_signals={len(run.signals)}"
                )
                for signal in run.signals:
                    _print_signal(signal)
                for reason in run.skipped:
                    print(f"  ~ skipped: {reason}")
            sys.stdout.flush()

        return await _run_cycles(
            follow=args.follow,
            once=pipeline.run_cycle,
            forever=lambda on_cycle, on_error, stop: pipeline.follow(on_cycle, on_error, stop=stop),
            report=report,
            ok=lambda outcome: outcome.market.ok,
        )


async def _strategy_replay(args: argparse.Namespace) -> int:
    from arbitron.domain.market import Timeframe  # noqa: PLC0415
    from arbitron.domain.portfolio import PortfolioView  # noqa: PLC0415
    from arbitron.strategy.evaluation import config_of, replay  # noqa: PLC0415

    config = _require_config(args.config)
    strategy = _require_strategy(config)
    timeframe = Timeframe(config.strategy.timeframe)
    symbol: str = args.symbol or config.exchange.symbols[0]
    if symbol not in config.exchange.symbols:
        print(f"error: {symbol} is not in the configured symbols", file=sys.stderr)
        return EXIT_INVALID_CONFIG
    async with _runtime(config, venue=False) as rt, rt.uow_factory() as tx:
        latest = await tx.candles.latest_closed(config.exchange.id, symbol, timeframe)
        if latest is None:
            print(f"no stored candles for {symbol} [{timeframe}]")
            return EXIT_OK
        end = args.end or latest.close_time
        start = args.start or end - 1000 * timeframe.duration
        # Warm-up: include the lookback before ``start`` so its first candle can be evaluated.
        candles = await tx.candles.closed_range(
            config.exchange.id,
            symbol,
            timeframe,
            start=start - (strategy.lookback - 1) * timeframe.duration,
            end=end,
        )
        print(
            f"replay {strategy.strategy_id} v{strategy.version} "
            f"config={config_of(strategy).config_hash[:12]} {symbol} [{timeframe}] "
            f"{start.isoformat()} .. {end.isoformat()} ({len(candles)} candles incl. warm-up)"
        )
        count = 0
        for signal in replay(
            strategy, candles, timeframe=timeframe, portfolio=PortfolioView(as_of=rt.clock.now())
        ):
            if signal.source_candle_time >= start:
                _print_signal(signal)
                count += 1
        print(f"{count} signal(s); nothing was written")
    return EXIT_OK


@asynccontextmanager
async def _kill_switch_service() -> AsyncGenerator["KillSwitchService"]:
    from arbitron.application.kill_switch import KillSwitchService  # noqa: PLC0415
    from arbitron.clocks import SystemClock  # noqa: PLC0415
    from arbitron.persistence.db import create_engine, create_session_factory  # noqa: PLC0415
    from arbitron.persistence.unit_of_work import SqlUnitOfWork  # noqa: PLC0415

    url = _database_url()
    if url is None:
        raise _CommandExit(EXIT_DATABASE_ERROR)
    engine = create_engine(url, pool_size=1)
    try:
        if (failure := await _schema_failure(engine)) is not None:
            raise _CommandExit(failure)
        factory = create_session_factory(engine)
        yield KillSwitchService(lambda: SqlUnitOfWork(factory), SystemClock())
    finally:
        await engine.dispose()


def _print_kill_switch(state: "KillSwitch") -> None:
    trading = "ALLOWED" if state.allows_new_orders else "BLOCKED"
    print(f"kill switch: {state.state.value} (new orders {trading})")
    print(f"  since:  {state.changed_at.isoformat()}")
    print(f"  by:     {state.actor}")
    print(f"  reason: {state.reason}")


async def _kill_switch(args: argparse.Namespace) -> int:
    from arbitron.domain.errors import DomainError  # noqa: PLC0415

    async with _kill_switch_service() as service:
        try:
            match args.kill_switch_command:
                case "status":
                    state = await service.ensure_initialized()
                case "trip":
                    state = await service.trip(reason=args.reason, actor=args.operator)
                case "review":
                    state = await service.begin_review(reason=args.reason, actor=args.operator)
                case _:
                    state = await service.arm(operator=args.operator, acknowledgement=args.ack)
        except DomainError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_INVALID_STATE
        _print_kill_switch(state)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arbitron",
        description="Autonomous spot crypto trading bot (paper and backtest first, fail closed).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    check = subcommands.add_parser("check-config", help="validate a configuration file")
    check.add_argument("config", type=Path, help="path to a TOML config file")
    check.set_defaults(handler=_check_config)

    db = subcommands.add_parser("db", help="database schema management")
    db_commands = db.add_subparsers(dest="db_command", required=True, metavar="DB_COMMAND")
    db_commands.add_parser("upgrade", help="apply migrations up to head").set_defaults(
        handler=_db_upgrade
    )
    db_commands.add_parser(
        "status", help="verify the schema is at the head this build expects"
    ).set_defaults(handler=_db_status)

    market = subcommands.add_parser("market", help="market data ingestion")
    market_commands = market.add_subparsers(
        dest="market_command", required=True, metavar="MARKET_COMMAND"
    )
    sync = market_commands.add_parser(
        "sync", help="ingest closed candles for the configured symbols and check data quality"
    )
    sync.add_argument("config", type=Path, help="path to a TOML config file")
    sync.add_argument("--follow", action="store_true", help="keep syncing after every candle close")
    sync.add_argument(
        "--lookback",
        type=int,
        default=100,
        help="candles of contiguous history the quality gate requires (default 100)",
    )
    sync.set_defaults(handler=_market_sync)

    strategy = subcommands.add_parser("strategy", help="strategy evaluation")
    strategy_commands = strategy.add_subparsers(
        dest="strategy_command", required=True, metavar="STRATEGY_COMMAND"
    )
    run = strategy_commands.add_parser(
        "run", help="sync market data, then evaluate the strategy on new closed candles"
    )
    run.add_argument("config", type=Path, help="path to a TOML config file")
    run.add_argument("--follow", action="store_true", help="repeat after every candle close")
    run.set_defaults(handler=_strategy_run)
    replay_cmd = strategy_commands.add_parser(
        "replay", help="print the signals the strategy produces over stored candles (no writes)"
    )
    replay_cmd.add_argument("config", type=Path, help="path to a TOML config file")
    replay_cmd.add_argument("--symbol", help="defaults to the first configured symbol")
    replay_cmd.add_argument(
        "--start", type=_utc_timestamp, help="ISO-8601 UTC start (default: 1000 candles back)"
    )
    replay_cmd.add_argument(
        "--end", type=_utc_timestamp, help="ISO-8601 UTC end, exclusive (default: latest)"
    )
    replay_cmd.set_defaults(handler=_strategy_replay)

    kill = subcommands.add_parser(
        "killswitch", help="show or change the durable kill switch (new orders need ARMED)"
    )
    kill_commands = kill.add_subparsers(
        dest="kill_switch_command", required=True, metavar="KILLSWITCH_COMMAND"
    )
    kill_commands.add_parser("status", help="show the current state").set_defaults(
        handler=_kill_switch
    )
    trip = kill_commands.add_parser("trip", help="emergency stop: block all new orders")
    trip.add_argument("--operator", required=True, help="who is tripping it (audit trail)")
    trip.add_argument("--reason", required=True)
    trip.set_defaults(handler=_kill_switch)
    review = kill_commands.add_parser("review", help="move a tripped switch to recovery review")
    review.add_argument("--operator", required=True)
    review.add_argument("--reason", required=True)
    review.set_defaults(handler=_kill_switch)
    arm = kill_commands.add_parser("arm", help="allow new orders again (only from recovery review)")
    arm.add_argument("--operator", required=True, help="named operator taking responsibility")
    arm.add_argument("--ack", required=True, help="what was reviewed and why it is safe")
    arm.set_defaults(handler=_kill_switch)

    return parser


def _utc_timestamp(value: str) -> "datetime":
    from datetime import UTC, datetime  # noqa: PLC0415

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        msg = f"invalid ISO-8601 timestamp: {value!r}"
        raise argparse.ArgumentTypeError(msg) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler: Command = args.handler

    async def run() -> int:
        try:
            return await handler(args)
        except _CommandExit as exc:
            return exc.code

    return asyncio.run(run())
