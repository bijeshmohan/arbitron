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

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler: Command = args.handler
    return asyncio.run(handler(args))
