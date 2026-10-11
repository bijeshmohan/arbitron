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

from pydantic import ValidationError

from arbitron import __version__
from arbitron.config import Secrets, load_config

EXIT_OK = 0
EXIT_INVALID_CONFIG = 1
EXIT_DATABASE_ERROR = 2

Command = Callable[[argparse.Namespace], Coroutine[None, None, int]]


def _format_validation_error(exc: ValidationError) -> str:
    lines: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  {location}: {error['msg']}")
    return "\n".join(lines)


async def _check_config(args: argparse.Namespace) -> int:
    path: Path = args.config
    try:
        config = load_config(path)
    except OSError as exc:
        print(f"error: cannot read {path}: {exc.strerror or exc}", file=sys.stderr)
        return EXIT_INVALID_CONFIG
    except tomllib.TOMLDecodeError as exc:
        print(f"error: {path} is not valid TOML: {exc}", file=sys.stderr)
        return EXIT_INVALID_CONFIG
    except ValidationError as exc:
        print(f"error: {path} is invalid:\n{_format_validation_error(exc)}", file=sys.stderr)
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

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler: Command = args.handler
    return asyncio.run(handler(args))
