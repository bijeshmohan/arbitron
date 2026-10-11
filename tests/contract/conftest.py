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

BINANCE_ROUTES: dict[str, str] = {
    "/api/v3/exchangeInfo": "exchange_info.json",
    "/api/v3/klines": "klines_btcusdt_5m.json",
    "/api/v3/ticker/24hr": "ticker_24hr_btcusdt.json",
    "/api/v3/time": "time.json",
}


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


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
