"""Command-line entry point.

Commands are ``async`` and dispatched through a single ``asyncio.run`` so the
application is async from the first command onward.
"""

import argparse
import asyncio
import sys
import tomllib
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from arbitron import __version__
from arbitron.config import ArbitronConfig, Secrets, load_config

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

EXIT_OK = 0
EXIT_INVALID_CONFIG = 1
EXIT_DATABASE_ERROR = 2
EXIT_DATA_QUALITY = 3
EXIT_VENUE_ERROR = 4

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

    secrets = Secrets()
    print(f"config OK: {path}")
    print(f"  mode:        {config.app.mode}")
    print(f"  exchange:    {config.exchange.id} (sandbox={config.exchange.sandbox})")
    print(f"  symbols:     {', '.join(config.exchange.symbols)}")
    print(
        f"  strategy:    {config.strategy.id} v{config.strategy.version} "
        f"[{config.strategy.timeframe}]"
    )
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


async def _market_sync(args: argparse.Namespace) -> int:
    import signal  # noqa: PLC0415

    from arbitron.clocks import SystemClock  # noqa: PLC0415
    from arbitron.exchange.ccxt_adapter import create_market_data  # noqa: PLC0415
    from arbitron.market.service import CycleOutcome, MarketDataService  # noqa: PLC0415
    from arbitron.persistence.db import create_engine, create_session_factory  # noqa: PLC0415
    from arbitron.persistence.unit_of_work import SqlUnitOfWork  # noqa: PLC0415
    from arbitron.ports.errors import VenueError  # noqa: PLC0415

    config = _load_config(args.config)
    if config is None:
        return EXIT_INVALID_CONFIG
    url = _database_url()
    if url is None:
        return EXIT_DATABASE_ERROR

    engine = create_engine(url)
    try:
        if (failure := await _schema_failure(engine)) is not None:
            return failure

        clock = SystemClock()
        factory = create_session_factory(engine)
        market_data = create_market_data(config, clock)
        service = MarketDataService(
            config,
            market_data=market_data,
            uow_factory=lambda: SqlUnitOfWork(factory),
            clock=clock,
            lookback=args.lookback,
        )

        def report(outcome: CycleOutcome) -> None:
            for result, quality in zip(outcome.results, outcome.reports, strict=True):
                latest = result.latest_closed.isoformat() if result.latest_closed else "-"
                status = "OK" if quality.ok else "BLOCKED"
                print(
                    f"{result.symbol} [{service.timeframe}] fetched={result.fetched} "
                    f"written={result.written} latest_closed={latest} quality={status}"
                )
                for issue in (*result.issues, *quality.issues):
                    marker = "!" if issue.blocking else "~"
                    print(f"  {marker} {issue.code.value}: {issue.detail}")
            sys.stdout.flush()

        def report_error(exc: VenueError) -> None:
            print(f"error: venue request failed: {exc}", file=sys.stderr)

        try:
            if not args.follow:
                outcome = await service.run_cycle()
                report(outcome)
                return EXIT_OK if outcome.ok else EXIT_DATA_QUALITY
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
            await service.follow(report, report_error, stop=stop)
            return EXIT_OK
        except VenueError as exc:
            report_error(exc)
            return EXIT_VENUE_ERROR
        finally:
            await market_data.close()
    finally:
        await engine.dispose()


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

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler: Command = args.handler
    return asyncio.run(handler(args))
