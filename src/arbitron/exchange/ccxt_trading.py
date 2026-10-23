"""CCXT implementation of ``ExecutionPort`` and ``AccountPort`` (authenticated).

Sandbox only in this build: ``create_trading`` refuses any configuration that
is not ``app.mode = "sandbox"`` with ``exchange.sandbox = true``, and checks
that CCXT really points at the venue's sandbox before returning (plan §18:
live order paths stay disabled until the Phase 12 review).

Rules (plan §8, §12):

- Reads are retried on retryable errors; order submission and cancellation
  are sent exactly once (``exchange.calls``).
- Amounts and prices must already be on the venue's step grid. CCXT would
  otherwise silently truncate them, so the venue order would differ from the
  recorded intent; such an intent is refused before anything is sent.
- The canonical client order ID is sent as the venue's client order ID, so
  an ambiguous submission can be looked up by it.
- "Order not found" from a lookup is ``None``, never an error.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import ccxt
import ccxt.async_support as ccxt_async

from arbitron.config.settings import ArbitronConfig, Mode, Secrets
from arbitron.domain.enums import OrderType
from arbitron.domain.fills import ExchangeTrade
from arbitron.domain.orders import ExchangeOrder, OrderIntent
from arbitron.domain.portfolio import Balance
from arbitron.exchange.calls import VenueCaller
from arbitron.exchange.capabilities import VenueCapabilities
from arbitron.exchange.ccxt_adapter import build_exchange
from arbitron.exchange.mapping import (
    balances_from_ccxt,
    datetime_from_ms,
    datetime_to_ms,
    exchange_order_from_ccxt,
    exchange_trade_from_ccxt,
)
from arbitron.exchange.retry import RetryPolicy, Sleep
from arbitron.ports.clock import Clock
from arbitron.ports.errors import (
    AuthenticationFailure,
    InvalidOrder,
    InvalidVenueData,
    MarketUnavailable,
    UnknownExchangeError,
    UnsupportedCapability,
)

# The only endpoints an authenticated adapter may talk to in this build.
SANDBOX_API_PREFIXES: Mapping[str, str] = {"binance": "https://testnet.binance.vision/"}
# Binance spot rejects own-trade queries spanning more than 24 hours.
TRADE_WINDOW = timedelta(hours=24)
TRADE_PAGE_LIMIT = 1000
DUPLICATE_ORDER_MESSAGE = "Duplicate order sent"


class CcxtTrading:
    def __init__(
        self,
        exchange: ccxt_async.Exchange,
        *,
        exchange_id: str,
        symbols: Sequence[str],
        clock: Clock,
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
        self._calls = VenueCaller(
            request_timeout=request_timeout,
            retry=retry,
            concurrency=concurrency,
            sleep=sleep,
            rng=rng,
        )
        self.capabilities = VenueCapabilities.from_ccxt_has(exchange.has)
        self._markets_ready = False

    @property
    def observer(self) -> Callable[[str, str, float], None] | None:
        return self._calls.observer

    @observer.setter
    def observer(self, value: Callable[[str, str, float], None] | None) -> None:
        self._calls.observer = value

    # --- account -----------------------------------------------------------------

    async def fetch_balances(self) -> Mapping[str, Balance]:
        raw = await self._calls.read("fetch_balance", self._exchange.fetch_balance)
        return balances_from_ccxt(raw, observed_at=self._clock.now())

    # --- orders ------------------------------------------------------------------

    async def submit_order(self, intent: OrderIntent) -> ExchangeOrder:
        self._require_symbol(intent.symbol)
        await self._ensure_markets()
        params: dict[str, Any] = {"newClientOrderId": intent.client_order_id}
        price: float | None = None
        if intent.order_type is OrderType.LIMIT:
            if intent.time_in_force is None or intent.limit_price is None:
                msg = "limit orders need a limit price and an explicit time in force"
                raise InvalidOrder(msg)
            params["timeInForce"] = intent.time_in_force.value.upper()
            self._require_on_grid(intent.symbol, "price", intent.limit_price)
            price = float(intent.limit_price)
        self._require_on_grid(intent.symbol, "amount", intent.amount)
        try:
            raw = await self._calls.write(
                "create_order",
                lambda: self._exchange.create_order(
                    intent.symbol,
                    intent.order_type.value,
                    intent.side.value,
                    float(intent.amount),
                    price,
                    params,
                ),
            )
        except InvalidOrder as exc:
            # Binance -2010 "Duplicate order sent": an order with this client ID
            # is open, so this intent's order may exist. CCXT reports it as a
            # plain InvalidOrder, which would read as "definitely not placed".
            if DUPLICATE_ORDER_MESSAGE in str(exc):
                msg = f"duplicate client order id {intent.client_order_id}: {exc}"
                raise UnknownExchangeError(msg) from exc
            raise
        # A response that fails to parse raises InvalidVenueData: the order may
        # exist, and the order manager treats it as an unknown outcome.
        return self._order(raw)

    async def cancel_order(self, exchange_order_id: str, symbol: str) -> ExchangeOrder | None:
        self._require_symbol(symbol)
        raw = await self._not_found_is_none(
            self._calls.write(
                "cancel_order", lambda: self._exchange.cancel_order(exchange_order_id, symbol)
            )
        )
        if raw is None:
            # Already closed (filled, expired, canceled): report its final state.
            return await self.fetch_order(exchange_order_id, symbol)
        return self._order(raw)

    async def fetch_order(self, exchange_order_id: str, symbol: str) -> ExchangeOrder | None:
        self._require_symbol(symbol)
        raw = await self._not_found_is_none(
            self._calls.read(
                "fetch_order", lambda: self._exchange.fetch_order(exchange_order_id, symbol)
            )
        )
        return self._order(raw) if raw is not None else None

    async def fetch_order_by_client_id(
        self, client_order_id: str, symbol: str
    ) -> ExchangeOrder | None:
        self._require_symbol(symbol)
        raw = await self._not_found_is_none(
            self._calls.read(
                "fetch_order",
                lambda: self._exchange.fetch_order(
                    "", symbol, {"origClientOrderId": client_order_id}
                ),
            )
        )
        if raw is None:
            return None
        order = self._order(raw)
        if order.client_order_id != client_order_id:
            msg = f"lookup by client id {client_order_id} returned {order.client_order_id}"
            raise InvalidVenueData(msg)
        return order

    async def fetch_open_orders(self, symbol: str | None = None) -> Sequence[ExchangeOrder]:
        """Open orders on the configured symbols (or one of them).

        Queried per symbol: an account-wide query costs far more rate-limit
        weight on Binance. Orders on symbols outside the allowlist are not seen.
        """
        symbols = (symbol,) if symbol is not None else self._symbols
        orders: list[ExchangeOrder] = []
        for s in symbols:
            self._require_symbol(s)
            rows = await self._calls.read(
                "fetch_open_orders",
                lambda s=s: self._exchange.fetch_open_orders(s),
            )
            orders.extend(self._order(row) for row in rows)
        return orders

    async def fetch_trades(
        self,
        symbol: str,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Sequence[ExchangeTrade]:
        """Own trades from ``since`` (default: the last 24 h) until now, ascending.

        Walks the range in windows the venue accepts, paging within a window
        when it is full. ``limit`` caps the total returned.
        """
        self._require_symbol(symbol)
        await self._ensure_markets()
        now = self._clock.now()
        start = since if since is not None else now - TRADE_WINDOW
        trades: dict[str, ExchangeTrade] = {}
        while start <= now:
            end = min(start + TRADE_WINDOW - timedelta(milliseconds=1), now)
            page = await self._trade_page(symbol, start, end)
            for trade in page:
                trades.setdefault(trade.dedupe_key, trade)
            if limit is not None and len(trades) >= limit:
                break
            if len(page) >= TRADE_PAGE_LIMIT:
                # Full page: continue from the last trade's time (duplicates collapse).
                last = page[-1].timestamp
                if last <= start:
                    msg = f"{symbol}: more than {TRADE_PAGE_LIMIT} trades in one millisecond"
                    raise InvalidVenueData(msg)
                start = last
                continue
            start = end + timedelta(milliseconds=1)
        ordered = sorted(trades.values(), key=lambda t: (t.timestamp, t.trade_id or ""))
        return ordered[:limit] if limit is not None else ordered

    async def _trade_page(self, symbol: str, start: datetime, end: datetime) -> list[ExchangeTrade]:
        rows = await self._calls.read(
            "fetch_my_trades",
            lambda: self._exchange.fetch_my_trades(
                symbol, datetime_to_ms(start), TRADE_PAGE_LIMIT, {"endTime": datetime_to_ms(end)}
            ),
        )
        return [exchange_trade_from_ccxt(row, exchange_id=self.exchange_id) for row in rows]

    async def fetch_time(self) -> datetime:
        value = await self._calls.read("fetch_time", self._exchange.fetch_time)
        if value is None:
            msg = "venue returned no server time"
            raise InvalidVenueData(msg)
        return datetime_from_ms(value, "server time")

    async def close(self) -> None:
        await self._exchange.close()

    # --- helpers -----------------------------------------------------------------

    def _require_symbol(self, symbol: str) -> None:
        if symbol not in self._symbols:
            msg = f"{symbol} is not in the configured symbol allowlist"
            raise MarketUnavailable(msg)

    async def _ensure_markets(self) -> None:
        if not self._markets_ready:
            await self._calls.read("load_markets", self._exchange.load_markets)
            self._markets_ready = True

    def _require_on_grid(self, symbol: str, field: str, value: Decimal) -> None:
        to_precision = (
            self._exchange.price_to_precision
            if field == "price"
            else self._exchange.amount_to_precision
        )
        try:
            venue_value = Decimal(str(to_precision(symbol, float(value))))
        except ccxt.BaseError as exc:
            msg = f"{symbol} {field} {value}: {exc}"
            raise InvalidOrder(msg) from exc
        if venue_value != value:
            msg = (
                f"{symbol} {field} {value} is not on the venue's step grid "
                f"(would be sent as {venue_value})"
            )
            raise InvalidOrder(msg)

    def _order(self, raw: Mapping[str, object]) -> ExchangeOrder:
        return exchange_order_from_ccxt(raw, exchange_id=self.exchange_id)

    @staticmethod
    async def _not_found_is_none[T](call: Awaitable[T]) -> T | None:
        try:
            return await call
        except InvalidOrder as exc:
            if isinstance(exc.__cause__, ccxt.OrderNotFound):
                return None
            raise


def create_trading(
    config: ArbitronConfig,
    clock: Clock,
    *,
    secrets: Secrets | None = None,
    exchange: ccxt_async.Exchange | None = None,
) -> CcxtTrading:
    """The authenticated adapter for the configured venue: sandbox only.

    ``exchange`` lets tests inject a CCXT instance with a stubbed transport.
    """
    if config.app.mode is not Mode.SANDBOX or not config.exchange.sandbox:
        msg = "authenticated venue access is only available in sandbox mode in this build"
        raise UnsupportedCapability(msg)
    exchange_id = config.exchange.id
    if exchange is None:
        secrets = secrets or Secrets()
        if secrets.exchange_api_key is None or secrets.exchange_api_secret is None:
            msg = "ARBITRON_EXCHANGE_API_KEY and ARBITRON_EXCHANGE_API_SECRET must be set"
            raise AuthenticationFailure(msg)
        exchange = build_exchange(
            config,
            credentials={
                "apiKey": secrets.exchange_api_key.get_secret_value(),
                "secret": secrets.exchange_api_secret.get_secret_value(),
            },
        )
    _require_sandbox_endpoints(exchange, exchange_id)
    adapter = CcxtTrading(
        exchange,
        exchange_id=exchange_id,
        symbols=config.exchange.symbols,
        clock=clock,
        request_timeout=timedelta(milliseconds=config.exchange.request_timeout_ms),
        retry=RetryPolicy(attempts=config.market.read_retry_attempts),
        concurrency=config.market.request_concurrency,
    )
    adapter.capabilities.require(
        "fetch_balance",
        "fetch_order",
        "fetch_open_orders",
        "fetch_my_trades",
        "create_order",
        "cancel_order",
    )
    return adapter


def _require_sandbox_endpoints(exchange: ccxt_async.Exchange, exchange_id: str) -> None:
    """Defense in depth: every private and public API URL must be the sandbox's."""
    prefix = SANDBOX_API_PREFIXES.get(exchange_id)
    urls = cast("dict[str, object]", exchange.urls.get("api") or {})
    used = [urls.get(key) for key in ("public", "private")]
    if prefix is None or not all(isinstance(u, str) and u.startswith(prefix) for u in used):
        msg = f"{exchange_id} is not pointed at its sandbox; refusing authenticated access"
        raise UnsupportedCapability(msg)
