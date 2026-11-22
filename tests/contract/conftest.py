"""Contract-test harness: real CCXT exchange classes with a canned HTTP transport.

CCXT's own parsing (precision modes, symbol unification, OHLCV shape) runs for
real against recorded venue responses; only the network is replaced.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import ccxt.async_support as ccxt_async

FIXTURES = Path(__file__).parents[1] / "fixtures" / "binance"
HL_FIXTURES = Path(__file__).parents[1] / "fixtures" / "hyperliquid"

BINANCE_ROUTES: dict[str, str] = {
    "/api/v3/exchangeInfo": "exchange_info.json",
    "/api/v3/klines": "klines_btcusdt_5m.json",
    "/api/v3/ticker/24hr": "ticker_24hr_btcusdt.json",
    "/api/v3/time": "time.json",
}


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def load_hl_fixture(name: str) -> Any:
    return json.loads((HL_FIXTURES / name).read_text())


def hl_fixture_name(body: dict[str, Any]) -> str:
    """Same rule as ``tests/fixtures/hyperliquid/record.py``."""
    kind = str(body["type"])
    coin = body.get("coin") or body.get("req", {}).get("coin")
    return f"{kind}_{coin}.json" if coin else f"{kind}.json"


class HyperliquidTransport:
    """Replaces ``Exchange.fetch`` for Hyperliquid, whose public API is one ``POST /info``.

    Requests route by the JSON body. ``use`` swaps the fixture for one request
    kind; unknown requests fail loudly.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.substitutes: dict[str, str] = {}

    def use(self, kind: str, fixture: str) -> None:
        self.substitutes[kind] = fixture

    async def __call__(
        self, url: str, method: str = "GET", headers: Any = None, body: Any = None
    ) -> Any:
        if urlsplit(url).path != "/info" or body is None:
            msg = f"unexpected venue request: {method} {url}"
            raise AssertionError(msg)
        payload = json.loads(body)
        self.requests.append(payload)
        name = self.substitutes.get(str(payload["type"])) or hl_fixture_name(payload)
        if not (HL_FIXTURES / name).exists():
            msg = f"no fixture {name} for request {payload}"
            raise AssertionError(msg)
        return load_hl_fixture(name)


class CannedTransport:
    """Replaces ``Exchange.fetch``. Records every request; unknown paths fail loudly."""

    def __init__(self, routes: dict[str, str]) -> None:
        self.routes = dict(routes)
        self.requests: list[str] = []
        # path -> callable raising an exception (or returning a payload override)
        self.overrides: dict[str, Callable[[], Any]] = {}

    async def __call__(
        self, url: str, method: str = "GET", headers: Any = None, body: Any = None
    ) -> Any:
        path = urlsplit(url).path
        self.requests.append(url)
        if path in self.overrides:
            return self.overrides[path]()
        if path not in self.routes:
            msg = f"unexpected venue request: {method} {url}"
            raise AssertionError(msg)
        return load_fixture(self.routes[path])


def binance_with_transport(options: dict[str, Any]) -> tuple[ccxt_async.Exchange, CannedTransport]:
    exchange = ccxt_async.binance({"enableRateLimit": False, "options": dict(options)})
    transport = CannedTransport(BINANCE_ROUTES)
    exchange.fetch = transport  # type: ignore[method-assign]
    return exchange, transport


def hyperliquid_with_transport(
    options: dict[str, Any],
) -> tuple[ccxt_async.Exchange, HyperliquidTransport]:
    exchange = ccxt_async.hyperliquid({"enableRateLimit": False, "options": dict(options)})
    transport = HyperliquidTransport()
    exchange.fetch = transport  # type: ignore[method-assign]
    return exchange, transport
