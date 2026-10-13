"""Command-line entry point.

Commands are ``async`` and dispatched through a single ``asyncio.run`` so the
application is async from the first command onward.
"""

import argparse
import asyncio
import signal as signal_module
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
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import AsyncEngine

    from arbitron.application.kill_switch import KillSwitchService
    from arbitron.application.paper_session import PaperSession, SessionOutcome
    from arbitron.domain.risk import KillSwitch
    from arbitron.domain.signals import Signal
    from arbitron.exchange.ccxt_adapter import CcxtMarketData
    from arbitron.execution.reconciler import ReconciliationReport
    from arbitron.market.service import CycleOutcome
    from arbitron.persistence.lock import InstanceLock
    from arbitron.ports.clock import Clock
    from arbitron.ports.errors import VenueError
    from arbitron.ports.notifier import Notifier
    from arbitron.ports.repositories import UnitOfWork
    from arbitron.strategy.base import Strategy

# argparse exposes no public type for the object add_subparsers() returns.
type _Subcommands = argparse._SubParsersAction[argparse.ArgumentParser]  # pyright: ignore[reportPrivateUsage]

EXIT_OK = 0
EXIT_INVALID_CONFIG = 1
EXIT_DATABASE_ERROR = 2
EXIT_DATA_QUALITY = 3
EXIT_VENUE_ERROR = 4
EXIT_INVALID_STATE = 5
EXIT_SESSION_FAILED = 6

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


def _print_reconciliation(report: "ReconciliationReport", *, label: str) -> None:
    status = "clean" if report.clean else "NOT CLEAN (new orders blocked)"
    print(f"  {label}: {status}")
    for line in report.resolved:
        print(f"    resolved: {line}")
    for line in report.repairs:
        print(f"    repaired: {line}")
    for line in report.pending:
        print(f"    ~ pending: {line}")
    for line in report.discrepancies:
        print(f"    ! discrepancy: {line}")


def _d(value: "Decimal") -> str:
    from arbitron.domain.money import canonical_str  # noqa: PLC0415

    return canonical_str(value)


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


def _secret_values() -> list[str]:
    secrets = Secrets()
    values = [
        secrets.exchange_api_key,
        secrets.exchange_api_secret,
        secrets.exchange_api_password,
        secrets.database_url,
        secrets.telegram_bot_token,
    ]
    return [v.get_secret_value() for v in values if v is not None]


def _require_notifier(config: ArbitronConfig) -> "Notifier":
    """The configured alert channel; refuses Telegram without its credentials."""
    from arbitron.config.settings import AlertChannel  # noqa: PLC0415
    from arbitron.monitoring.alerts import LogNotifier, TelegramNotifier  # noqa: PLC0415

    if config.alerts.channel is AlertChannel.LOG:
        return LogNotifier()
    secrets = Secrets()
    if secrets.telegram_bot_token is None or secrets.telegram_chat_id is None:
        print(
            "error: [alerts] channel is 'telegram' but ARBITRON_TELEGRAM_BOT_TOKEN and "
            "ARBITRON_TELEGRAM_CHAT_ID are not both set",
            file=sys.stderr,
        )
        raise _CommandExit(EXIT_INVALID_CONFIG)
    return TelegramNotifier(
        bot_token=secrets.telegram_bot_token.get_secret_value(),
        chat_id=secrets.telegram_chat_id.get_secret_value(),
    )


def _print_session_cycle(outcome: "SessionOutcome", config: ArbitronConfig) -> None:
    _print_market(outcome.pipeline.market, config.strategy.timeframe)
    for run in outcome.pipeline.runs:
        for signal in run.signals:
            _print_signal(signal)
    if outcome.equity is not None:
        print(f"  equity: {_d(outcome.equity)} {config.exchange.quote_currency}")
    if outcome.tripped:
        print(f"  ! KILL SWITCH TRIPPED: {', '.join(r.value for r in outcome.tripped)}")
    for order in outcome.orders:
        status = order.order.status.value if order.order else "-"
        print(f"  order: {order.action.value} status={status} {order.detail}")
    for reason in outcome.skipped_signals:
        print(f"  ~ signal not traded: {reason}")
    if outcome.reconciliation is not None:
        _print_reconciliation(outcome.reconciliation, label="reconciliation")
    sys.stdout.flush()


async def _paper_run(args: argparse.Namespace) -> int:
    from arbitron.application.paper_runtime import PaperRuntime  # noqa: PLC0415
    from arbitron.application.paper_session import PaperSession  # noqa: PLC0415
    from arbitron.config.settings import Mode  # noqa: PLC0415
    from arbitron.monitoring.logging import configure_logging  # noqa: PLC0415
    from arbitron.persistence.db import create_engine, create_session_factory  # noqa: PLC0415
    from arbitron.persistence.lock import InstanceLock, LockNotAcquiredError  # noqa: PLC0415

    config = _require_config(args.config)
    if config.app.mode is not Mode.PAPER:
        print(f"error: paper run needs app.mode = 'paper', got {config.app.mode}", file=sys.stderr)
        return EXIT_INVALID_CONFIG
    strategy = _require_strategy(config)
    notifier = _require_notifier(config) if args.follow else None
    redactor = configure_logging(
        log_format=config.monitoring.log_format,
        level=config.monitoring.log_level,
        secrets=_secret_values(),
    )
    async with _runtime(config, venue=True) as rt:
        assert rt.market_data is not None  # noqa: S101 - venue=True
        url = _database_url()
        assert url is not None  # noqa: S101 - _runtime checked it
        lock_engine = create_engine(url, pool_size=2)
        lock = InstanceLock(
            lock_engine, exchange_id=config.exchange.id, account=config.paper.account, mode="paper"
        )
        try:
            await lock.acquire()
        except LockNotAcquiredError:
            await lock_engine.dispose()
            print("error: another paper session holds this account's lock", file=sys.stderr)
            return EXIT_INVALID_STATE
        try:
            session = PaperSession(
                config,
                strategy=strategy,
                market_data=rt.market_data,
                session_factory=create_session_factory(lock_engine),
                uow_factory=rt.uow_factory,
                clock=rt.clock,
            )
            if notifier is None:
                return await _paper_cycle_once(session, config, lock)

            runtime = PaperRuntime(
                config,
                session=session,
                notifier=notifier,
                uow_factory=rt.uow_factory,
                clock=rt.clock,
                lock=lock,
                redactor=redactor,
                on_cycle=lambda outcome: _print_session_cycle(outcome, config),
            )
            metrics = runtime.metrics

            def observe_venue(operation: str, result: str, seconds: float) -> None:
                metrics.venue_requests.labels(operation, result).inc()
                metrics.venue_latency.labels(operation).observe(seconds)

            rt.market_data.observer = observe_venue
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal_module.SIGINT, signal_module.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
            print(
                f"paper session running; /health /ready /metrics on "
                f"http://{config.monitoring.http_host}:{config.monitoring.http_port}"
            )
            outcome = await runtime.run(stop)
            if outcome.failure is not None:
                print(f"error: session stopped: {outcome.failure}", file=sys.stderr)
                return EXIT_SESSION_FAILED
            return EXIT_OK
        finally:
            close = getattr(notifier, "close", None)
            if close is not None:
                await close()
            await lock.release()
            await lock_engine.dispose()


async def _paper_cycle_once(
    session: "PaperSession", config: ArbitronConfig, lock: "InstanceLock"
) -> int:
    from arbitron.ports.errors import VenueError  # noqa: PLC0415

    startup = await session.start()
    if startup.funded:
        print(
            f"paper account {config.paper.account!r} funded with "
            f"{config.paper.starting_balance} {config.exchange.quote_currency}"
        )
    if startup.recovered:
        print(f"  ! {startup.recovered} order(s) were interrupted mid-submission")
    _print_reconciliation(startup.reconciliation, label="startup reconciliation")
    await lock.check()
    try:
        outcome = await session.run_cycle()
    except VenueError as exc:
        print(f"error: venue request failed: {exc}", file=sys.stderr)
        return EXIT_VENUE_ERROR
    _print_session_cycle(outcome, config)
    return EXIT_OK if outcome.pipeline.market.ok else EXIT_DATA_QUALITY


async def _alerts_test(args: argparse.Namespace) -> int:
    from arbitron.domain.enums import Severity  # noqa: PLC0415
    from arbitron.domain.events import Alert  # noqa: PLC0415
    from arbitron.monitoring.alerts import NotifierError  # noqa: PLC0415

    config = _require_config(args.config)
    notifier = _require_notifier(config)
    from datetime import UTC, datetime  # noqa: PLC0415

    alert = Alert(
        severity=Severity.CRITICAL,
        title="Test alert",
        message="This is a test from `arbitron alerts test`. No action is needed.",
        action_taken="None: this is a test.",
        remediation="If you can read this, alert delivery works.",
        dedupe_key="test",
        occurred_at=datetime.now(UTC),
    )
    try:
        await notifier.notify(alert)
    except NotifierError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_VENUE_ERROR
    finally:
        close = getattr(notifier, "close", None)
        if close is not None:
            await close()
    print(f"test alert sent via {config.alerts.channel.value}")
    return EXIT_OK


def _add_alerts(subcommands: "_Subcommands") -> None:
    alerts = subcommands.add_parser("alerts", help="operator alert channel")
    commands = alerts.add_subparsers(dest="alerts_command", required=True, metavar="ALERTS_COMMAND")
    test = commands.add_parser("test", help="send a test alert through the configured channel")
    test.add_argument("config", type=Path, help="path to a TOML config file")
    test.set_defaults(handler=_alerts_test)


async def _paper_status(args: argparse.Namespace) -> int:
    from arbitron.paper.broker import paper_balances  # noqa: PLC0415
    from arbitron.persistence.db import create_engine, create_session_factory  # noqa: PLC0415

    config = _require_config(args.config)
    quote = config.exchange.quote_currency
    async with _runtime(config, venue=False) as rt:
        url = _database_url()
        assert url is not None  # noqa: S101 - _runtime checked it
        engine = create_engine(url, pool_size=1)
        try:
            balances = await paper_balances(
                create_session_factory(engine), config.paper.account, observed_at=rt.clock.now()
            )
        finally:
            await engine.dispose()
        print(f"paper account {config.paper.account!r} (simulated; not a real account)")
        if not balances:
            print("  not funded yet: run `arbitron paper run` to create it")
        for asset, bal in sorted(balances.items()):
            print(
                f"  balance {asset}: total={_d(bal.total)} free={_d(bal.free)} used={_d(bal.used)}"
            )
        async with rt.uow_factory() as tx:
            positions = await tx.positions.list(config.exchange.id)
            equity = await tx.equity_snapshots.latest(quote)
            peak = await tx.equity_snapshots.peak(quote)
            recent = await tx.orders.list_recent(config.exchange.id, args.orders)
            kill_switch = await tx.kill_switch.get()
        for position in positions:
            print(
                f"  position {position.symbol}: qty={_d(position.quantity)} "
                f"avg_cost={_d(position.average_cost)} "
                f"realized_pnl={_d(position.realized_pnl)} fees={_d(position.fees_paid)} {quote}"
            )
        if equity is not None:
            peak_text = _d(peak) if peak is not None else "-"
            print(
                f"  equity {_d(equity.equity)} {quote} at {equity.taken_at.isoformat()} "
                f"(peak {peak_text})"
            )
        state = kill_switch.state.value if kill_switch else "uninitialized"
        print(f"  kill switch: {state}")
        for order in recent:
            intent = order.intent
            limit = _d(intent.limit_price) if intent.limit_price is not None else "market"
            average = _d(order.average_fill_price) if order.average_fill_price else "-"
            print(
                f"  order {intent.created_at.isoformat()} {intent.side.value.upper()} "
                f"{_d(intent.amount)} {intent.symbol} @ {limit} -> {order.status.value} "
                f"filled={_d(order.filled_amount)} avg={average}"
            )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arbitron",
        description="Autonomous spot crypto trading bot (paper and backtest first, fail closed).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    for add in (
        _add_check,
        _add_db,
        _add_market,
        _add_strategy,
        _add_kill_switch,
        _add_paper,
        _add_orders,
        _add_alerts,
    ):
        add(subcommands)
    return parser


def _add_check(subcommands: "_Subcommands") -> None:
    check = subcommands.add_parser("check-config", help="validate a configuration file")
    check.add_argument("config", type=Path, help="path to a TOML config file")
    check.set_defaults(handler=_check_config)


def _add_db(subcommands: "_Subcommands") -> None:
    db = subcommands.add_parser("db", help="database schema management")
    db_commands = db.add_subparsers(dest="db_command", required=True, metavar="DB_COMMAND")
    db_commands.add_parser("upgrade", help="apply migrations up to head").set_defaults(
        handler=_db_upgrade
    )
    db_commands.add_parser(
        "status", help="verify the schema is at the head this build expects"
    ).set_defaults(handler=_db_status)


def _add_market(subcommands: "_Subcommands") -> None:
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


def _add_strategy(subcommands: "_Subcommands") -> None:
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


def _add_kill_switch(subcommands: "_Subcommands") -> None:
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


async def _orders(args: argparse.Namespace) -> int:
    from uuid import UUID  # noqa: PLC0415

    from arbitron.application.order_resolution import (  # noqa: PLC0415
        ResolutionRefusedError,
        resolve_order,
    )
    from arbitron.application.paper_session import PaperSession  # noqa: PLC0415
    from arbitron.domain.enums import OrderStatus  # noqa: PLC0415
    from arbitron.persistence.db import create_engine, create_session_factory  # noqa: PLC0415

    config = _require_config(args.config)
    strategy = _require_strategy(config)
    async with _runtime(config, venue=True) as rt:
        assert rt.market_data is not None  # noqa: S101 - venue=True
        if args.orders_command == "unresolved":
            async with rt.uow_factory() as tx:
                pending = [
                    o
                    for o in await tx.orders.list_non_terminal(config.exchange.id)
                    if o.status
                    in (OrderStatus.SUBMISSION_UNKNOWN, OrderStatus.RECONCILIATION_REQUIRED)
                ]
            for order in pending:
                intent = order.intent
                print(
                    f"{order.id} {order.status.value} {intent.side.value} {_d(intent.amount)} "
                    f"{intent.symbol} client={intent.client_order_id} "
                    f"since={order.updated_at.isoformat()}"
                )
            if not pending:
                print("no unresolved orders")
            return EXIT_OK

        url = _database_url()
        assert url is not None  # noqa: S101 - _runtime checked it
        engine = create_engine(url, pool_size=1)
        try:
            session = PaperSession(
                config,
                strategy=strategy,
                market_data=rt.market_data,
                session_factory=create_session_factory(engine),
                uow_factory=rt.uow_factory,
                clock=rt.clock,
            )
            async with rt.uow_factory() as tx:
                order = await tx.orders.get(UUID(args.order_id))
            if order is None:
                print(f"error: no order {args.order_id}", file=sys.stderr)
                return EXIT_INVALID_STATE
            try:
                resolved = await resolve_order(
                    order=order,
                    outcome=OrderStatus(args.outcome),
                    operator=args.operator,
                    note=args.note,
                    execution=session.broker,
                    uow_factory=rt.uow_factory,
                    clock=rt.clock,
                )
            except ResolutionRefusedError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return EXIT_INVALID_STATE
        finally:
            await engine.dispose()
        print(f"order {resolved.id} resolved as {resolved.status.value} by {args.operator}")
        print(
            "  new orders stay blocked until reconciliation is clean and the kill switch is armed"
        )
    return EXIT_OK


def _add_orders(subcommands: "_Subcommands") -> None:
    orders = subcommands.add_parser("orders", help="inspect and resolve orders (operator)")
    commands = orders.add_subparsers(dest="orders_command", required=True, metavar="ORDERS_COMMAND")
    unresolved = commands.add_parser(
        "unresolved", help="orders awaiting reconciliation or an operator decision"
    )
    unresolved.add_argument("config", type=Path, help="path to a TOML config file")
    unresolved.set_defaults(handler=_orders)
    resolve = commands.add_parser(
        "resolve",
        help="close an unresolved order as unfilled after checking the venue has no record",
    )
    resolve.add_argument("config", type=Path, help="path to a TOML config file")
    resolve.add_argument("order_id", help="internal order id (from `orders unresolved`)")
    resolve.add_argument(
        "--as",
        dest="outcome",
        required=True,
        choices=["not_placed", "canceled", "expired", "exchange_rejected"],
    )
    resolve.add_argument("--operator", required=True, help="named operator (audit trail)")
    resolve.add_argument("--note", required=True, help="evidence for the resolution")
    resolve.set_defaults(handler=_orders)


def _add_paper(subcommands: "_Subcommands") -> None:
    paper = subcommands.add_parser("paper", help="paper trading (simulated venue, no real funds)")
    paper_commands = paper.add_subparsers(
        dest="paper_command", required=True, metavar="PAPER_COMMAND"
    )
    paper_run = paper_commands.add_parser(
        "run", help="market data -> strategy -> risk -> simulated orders, per candle close"
    )
    paper_run.add_argument("config", type=Path, help="path to a paper-mode TOML config")
    paper_run.add_argument("--follow", action="store_true", help="repeat after every candle close")
    paper_run.set_defaults(handler=_paper_run)
    paper_status = paper_commands.add_parser("status", help="paper balances, positions, orders")
    paper_status.add_argument("config", type=Path, help="path to a TOML config file")
    paper_status.add_argument("--orders", type=int, default=10, help="recent orders to show")
    paper_status.set_defaults(handler=_paper_status)


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
