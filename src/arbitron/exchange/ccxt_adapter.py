"""CCXT implementation of ``MarketDataPort`` (public, read-only endpoints).

The rest of the application never imports CCXT (plan §8). Every call here is
bounded (CCXT timeout plus an outer ``asyncio.timeout``), concurrency-limited,
rate-limited by CCXT, translated into the venue error taxonomy, and retried
only when the error is retryable.
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

import ccxt.async_support as ccxt_async

from arbitron.config.settings import ArbitronConfig
from arbitron.domain.market import Candle, Market, Ticker, Timeframe
from arbitron.exchange.capabilities import SUPPORTED_VENUES, VenueCapabilities
from arbitron.exchange.errors import venue_errors
from arbitron.exchange.mapping import (
    candles_from_ohlcv,
    datetime_from_ms,
    datetime_to_ms,
    market_from_ccxt,
    ticker_from_ccxt,
)
from arbitron.exchange.retry import RetryPolicy, Sleep, retry_read
from arbitron.ports.clock import Clock
from arbitron.ports.errors import InvalidVenueData, MarketUnavailable, UnsupportedCapability

# Venue-specific options live here and nowhere else (plan §8.2).
_VENUE_OPTIONS: Mapping[str, dict[str, Any]] = {
    "binance": {
        # Spot only: skip margin/futures/options market endpoints entirely.
        "fetchMarkets": {"types": ["spot"]},
        # Currency metadata needs authenticated endpoints and is not used.
        "fetchCurrencies": False,
    },
}
_VENUE_CLASSES: Mapping[str, type[ccxt_async.Exchange]] = {"binance": ccxt_async.binance}


class CcxtMarketData:
    def __init__(
        self,
        exchange: ccxt_async.Exchange,
        *,
        exchange_id: str,
        symbols: Sequence[str],
        clock: Clock,
        close_grace: timedelta,
        request_timeout: timedelta,
        retry: RetryPolicy,
        concurrency: int,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._exchange = exchange
        self.exchange_id = exchange_id
        self._symbols = tuple(symbols)
        self._clock = clock
        self._close_grace = close_grace
        # Outer guard slightly above CCXT's own timeout, so CCXT normally
        # reports first and this only catches a hung call.
        self._call_timeout = request_timeout.total_seconds() * 1.5
        self._retry = retry
        self._semaphore = asyncio.Semaphore(concurrency)
        self._sleep = sleep
        self._rng = rng
        self.capabilities = VenueCapabilities.from_ccxt_has(exchange.has)
        self.markets_loaded_at: datetime | None = None
        # (operation, outcome, seconds) per attempt, for metrics; set by the runtime.
        self.observer: Callable[[str, str, float], None] | None = None

    async def _call[T](self, operation: str, make_call: Callable[[], Awaitable[T]]) -> T:
        async def attempt() -> T:
            async with self._semaphore:
                started = time.monotonic()
                outcome = "ok"
                try:
                    with venue_errors():
                        async with asyncio.timeout(self._call_timeout):
                            return await make_call()
                except Exception as exc:
                    outcome = type(exc).__name__
                    raise
                finally:
                    if self.observer is not None:
                        self.observer(operation, outcome, time.monotonic() - started)

        return await retry_read(attempt, self._retry, sleep=self._sleep, rng=self._rng)

    def _require_symbol(self, symbol: str) -> None:
        if symbol not in self._symbols:
            msg = f"{symbol} is not in the configured symbol allowlist"
            raise MarketUnavailable(msg)

    async def load_markets(self) -> Mapping[str, Market]:
        raw = await self._call("load_markets", lambda: self._exchange.load_markets(reload=True))
        markets: dict[str, Market] = {}
        for symbol in self._symbols:
            entry = raw.get(symbol)
            if entry is None:
                msg = f"{symbol} is not listed on {self.exchange_id}"
                raise MarketUnavailable(msg)
            if entry.get("spot") is not True:
                msg = f"{symbol} on {self.exchange_id} is not a spot market"
                raise MarketUnavailable(msg)
            markets[symbol] = market_from_ccxt(
                entry, exchange_id=self.exchange_id, precision_mode=self._exchange.precisionMode
            )
        self.markets_loaded_at = self._clock.now()
        return markets

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Sequence[Candle]:
        self._require_symbol(symbol)
        since_ms = datetime_to_ms(since) if since is not None else None
        rows = await self._call(
            "fetch_ohlcv",
            lambda: self._exchange.fetch_ohlcv(symbol, timeframe.value, since_ms, limit),
        )
        return candles_from_ohlcv(
            rows,
            exchange_id=self.exchange_id,
            symbol=symbol,
            timeframe=timeframe,
            now=self._clock.now(),
            close_grace=self._close_grace,
        )

    async def fetch_ticker(self, symbol: str) -> Ticker:
        self._require_symbol(symbol)
        raw = await self._call("fetch_ticker", lambda: self._exchange.fetch_ticker(symbol))
        return ticker_from_ccxt(raw, exchange_id=self.exchange_id, symbol=symbol)

    async def fetch_time(self) -> datetime:
        value = await self._call("fetch_time", self._exchange.fetch_time)
        if value is None:
            msg = "venue returned no server time"
            raise InvalidVenueData(msg)
        return datetime_from_ms(value, "server time")

    async def close(self) -> None:
        await self._exchange.close()


def create_market_data(
    config: ArbitronConfig,
    clock: Clock,
    *,
    exchange: ccxt_async.Exchange | None = None,
) -> CcxtMarketData:
    """Build the market-data adapter for the configured venue.

    No credentials are passed: market data uses public endpoints only.
    ``exchange`` lets tests inject a CCXT instance with a stubbed transport.
    """
    exchange_id = config.exchange.id
    if exchange_id not in SUPPORTED_VENUES:
        msg = f"exchange {exchange_id!r} is not supported (supported: {sorted(SUPPORTED_VENUES)})"
        raise UnsupportedCapability(msg)
    if exchange is None:
        exchange = _VENUE_CLASSES[exchange_id](
            {
                "enableRateLimit": True,
                "timeout": config.exchange.request_timeout_ms,
                "options": dict(_VENUE_OPTIONS[exchange_id]),
            }
        )
        if config.exchange.sandbox:
            exchange.set_sandbox_mode(True)
    adapter = CcxtMarketData(
        exchange,
        exchange_id=exchange_id,
        symbols=config.exchange.symbols,
        clock=clock,
        close_grace=timedelta(seconds=config.market.close_grace_seconds),
        request_timeout=timedelta(milliseconds=config.exchange.request_timeout_ms),
        retry=RetryPolicy(attempts=config.market.read_retry_attempts),
        concurrency=config.market.request_concurrency,
    )
    adapter.capabilities.require_market_data()
    return adapter


def venue_options(exchange_id: str) -> Mapping[str, Any]:
    """Exposed for contract tests."""
    return _VENUE_OPTIONS[exchange_id]
