"""Sandbox qualification (plan §22, Phase 11): exercise the real venue adapter.

``check`` is read-only. ``order_lifecycle`` places, inspects, and cancels tiny
orders on the venue's *sandbox* (``create_trading`` refuses anything else):

1. precision: an off-grid amount is refused locally, before sending;
2. minimums: an order below the venue's minimum notional is rejected;
3. resting order: a GTC buy well below the market rests (``OPEN``) and is
   visible by exchange ID, by client order ID, and in open orders;
4. client IDs: re-sending the same client order ID while it is open is
   refused by the venue;
5. cancellation: cancel moves it to ``CANCELED``; cancelling again reports
   the final state instead of failing;
6. IOC: a non-marketable IOC expires unfilled; a marketable IOC buy and the
   matching sell fill, and the account's trades add up to the reported fills;
7. lookups of orders that never existed return "not found", not an error.

Each step records a result and the run continues where it safely can; any
order left open is cancelled at the end. Reconciliation against the sandbox
needs a database and is covered by the opt-in sandbox test suite.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from typing import Protocol

from arbitron.domain.enums import OrderStatus, OrderType, Side, TimeInForce
from arbitron.domain.fills import ExchangeTrade
from arbitron.domain.market import Market
from arbitron.domain.money import (
    FINANCIAL_CONTEXT,
    canonical_str,
    round_down_to_step,
    round_up_to_step,
)
from arbitron.domain.orders import ExchangeOrder, OrderIntent
from arbitron.market.freshness import measure_clock_offset
from arbitron.ports.account import AccountPort
from arbitron.ports.clock import Clock
from arbitron.ports.errors import (
    InvalidOrder,
    InvalidVenueData,
    UnknownExchangeError,
    VenueError,
)
from arbitron.ports.execution import ExecutionPort
from arbitron.ports.market_data import MarketDataPort

STRATEGY_ID = "sandbox-qualification"
# Orders are sized at this multiple of the venue's minimum notional.
NOTIONAL_MULTIPLE = Decimal(2)
# A resting buy sits this far below the bid (inside Binance's price band).
RESTING_DISCOUNT = Decimal("0.9")
# Marketable IOC orders cross the touch by this much.
IOC_CROSS = Decimal("0.001")


class SandboxVenue(ExecutionPort, AccountPort, Protocol):
    """The authenticated adapter (``exchange.ccxt_trading.CcxtTrading``)."""

    exchange_id: str


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


@dataclass
class QualificationReport:
    symbol: str
    results: list[CheckResult] = field(default_factory=list[CheckResult])

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results)

    def record(self, name: str, ok: bool, detail: str) -> CheckResult:
        result = CheckResult(name, ok, detail)
        self.results.append(result)
        return result


class SandboxQualification:
    def __init__(
        self,
        *,
        market_data: MarketDataPort,
        trading: SandboxVenue,
        clock: Clock,
        symbol: str,
        max_clock_skew: timedelta,
    ) -> None:
        self._data = market_data
        self._trading = trading
        self._clock = clock
        self._symbol = symbol
        self._max_skew = max_clock_skew
        self._open: set[str] = set()  # exchange order IDs that may still rest

    # --- read-only ---------------------------------------------------------------

    async def check(self) -> QualificationReport:
        report = QualificationReport(self._symbol)
        await self._step(report, "clock", self._check_clock)
        market = await self._step(report, "market", self._check_market)
        await self._step(report, "balances", lambda: self._check_balances(market))
        await self._step(report, "open orders", self._check_no_open_orders)
        return report

    async def _check_clock(self) -> tuple[bool, str]:
        offset = await measure_clock_offset(self._data, self._clock)
        ok = abs(offset) <= self._max_skew
        return ok, f"venue - local = {offset.total_seconds() * 1000:.0f} ms"

    async def _check_market(self) -> tuple[bool, str, Market]:
        market = (await self._data.load_markets())[self._symbol]
        return (
            market.active,
            f"active={market.active} amount_step={canonical_str(market.amount_step)} "
            f"price_step={canonical_str(market.price_step)} "
            f"min_amount={canonical_str(market.min_amount)} "
            f"min_cost={canonical_str(market.min_cost)}",
            market,
        )

    async def _check_balances(self, market: Market | None) -> tuple[bool, str]:
        balances = await self._trading.fetch_balances()
        assets = (market.base, market.quote) if market else ()
        shown = ", ".join(
            f"{a} free={canonical_str(balances[a].free)} used={canonical_str(balances[a].used)}"
            if a in balances
            else f"{a} none"
            for a in assets
        )
        return all(a in balances for a in assets), f"{len(balances)} assets held; {shown}"

    async def _check_no_open_orders(self) -> tuple[bool, str]:
        orders = await self._trading.fetch_open_orders()
        detail = ", ".join(f"{o.symbol} {o.exchange_order_id}" for o in orders) or "none"
        return not orders, f"open orders on the allowlist: {detail}"

    # --- order lifecycle ---------------------------------------------------------

    async def order_lifecycle(self) -> QualificationReport:
        report = await self.check()
        if not report.ok:
            report.record("lifecycle", False, "skipped: read-only checks failed")
            return report
        market = (await self._data.load_markets())[self._symbol]
        try:
            await self._lifecycle(report, market)
        finally:
            await self._cleanup(report)
        return report

    async def _lifecycle(self, report: QualificationReport, market: Market) -> None:
        await self._step(report, "precision refused locally", lambda: self._off_grid(market))
        await self._step(report, "minimum notional rejected", lambda: self._below_min(market))

        resting = await self._step(report, "resting GTC order", lambda: self._rest(market))
        if resting is not None:
            intent, order = resting
            await self._step(report, "lookups", lambda: self._lookups(intent, order))
            await self._step(report, "duplicate client id", lambda: self._duplicate(intent))
            await self._step(report, "cancel", lambda: self._cancel(order))
            await self._step(report, "cancel again", lambda: self._cancel_again(order))

        await self._step(report, "IOC expires unfilled", lambda: self._ioc_unfilled(market))
        bought = await self._step(
            report, "IOC buy fills", lambda: self._ioc_fill(market, Side.BUY, None)
        )
        if bought is not None:
            await self._step(
                report,
                "IOC sell fills",
                lambda: self._ioc_fill(market, Side.SELL, bought.filled),
            )
        await self._step(report, "unknown orders", self._unknown_lookups)

    async def _off_grid(self, market: Market) -> tuple[bool, str]:
        bid, _ = await self._touch()
        price = round_down_to_step(bid * RESTING_DISCOUNT, market.price_step)
        amount = self._amount_for(market, price) + market.amount_step / 2
        try:
            order = await self._submit(self._intent(Side.BUY, amount, price, TimeInForce.GTC))
        except InvalidOrder as exc:
            return "step grid" in str(exc), f"refused before sending: {exc}"
        self._open.add(order.exchange_order_id)
        return False, f"off-grid amount {amount} was sent and accepted as {order.amount}"

    async def _below_min(self, market: Market) -> tuple[bool, str]:
        _, ask = await self._touch()
        price = round_up_to_step(ask * (1 + IOC_CROSS), market.price_step)
        amount = max(market.min_amount, market.amount_step)
        with localcontext(FINANCIAL_CONTEXT):
            if amount * price >= market.min_cost:
                return True, "skipped: the minimum lot already meets the minimum notional"
        try:
            order = await self._submit(self._intent(Side.BUY, amount, price, TimeInForce.IOC))
        except InvalidOrder as exc:
            return True, f"venue rejected {amount} @ {price}: {exc}"
        return False, f"order below minimum notional was accepted: {order.raw_status}"

    async def _rest(self, market: Market) -> tuple[bool, str, tuple[OrderIntent, ExchangeOrder]]:
        bid, _ = await self._touch()
        price = round_down_to_step(bid * RESTING_DISCOUNT, market.price_step)
        intent = self._intent(Side.BUY, self._amount_for(market, price), price, TimeInForce.GTC)
        order = await self._submit(intent)
        self._open.add(order.exchange_order_id)
        ok = (
            order.status is OrderStatus.OPEN
            and order.client_order_id == intent.client_order_id
            and order.amount == intent.amount
            and order.filled == 0
        )
        return (
            ok,
            f"{order.raw_status} id={order.exchange_order_id} client_id={order.client_order_id} "
            f"{canonical_str(order.amount)} @ {canonical_str(price)}",
            (intent, order),
        )

    async def _lookups(self, intent: OrderIntent, order: ExchangeOrder) -> tuple[bool, str]:
        by_id = await self._trading.fetch_order(order.exchange_order_id, self._symbol)
        by_client = await self._trading.fetch_order_by_client_id(
            intent.client_order_id, self._symbol
        )
        open_ids = {o.exchange_order_id for o in await self._trading.fetch_open_orders()}
        ok = (
            by_id is not None
            and by_client is not None
            and by_id.exchange_order_id == by_client.exchange_order_id == order.exchange_order_id
            and by_client.status is OrderStatus.OPEN
            and order.exchange_order_id in open_ids
        )
        return ok, (
            f"by id: {by_id and by_id.status}; by client id: {by_client and by_client.status}; "
            f"in open orders: {order.exchange_order_id in open_ids}"
        )

    async def _duplicate(self, intent: OrderIntent) -> tuple[bool, str]:
        try:
            order = await self._submit(intent)
        except UnknownExchangeError as exc:
            # The safe reading: the order manager treats it as "may exist" and
            # reconciles by client ID (a rejection would orphan the open order).
            return True, f"refused, outcome treated as unknown: {exc}"
        except InvalidOrder as exc:
            return False, f"refused as a definitive rejection (would orphan the order): {exc}"
        self._open.add(order.exchange_order_id)
        return False, f"duplicate accepted as a second order {order.exchange_order_id}"

    async def _cancel(self, order: ExchangeOrder) -> tuple[bool, str]:
        canceled = await self._trading.cancel_order(order.exchange_order_id, self._symbol)
        after = await self._trading.fetch_order(order.exchange_order_id, self._symbol)
        if after is not None and after.status is OrderStatus.CANCELED:
            self._open.discard(order.exchange_order_id)
        ok = (
            canceled is not None
            and canceled.status is OrderStatus.CANCELED
            and canceled.client_order_id == order.client_order_id
            and after is not None
            and after.status is OrderStatus.CANCELED
            and after.filled == 0
        )
        return ok, (
            f"cancel returned {canceled and canceled.raw_status} "
            f"(client id {canceled and canceled.client_order_id}); "
            f"re-fetched {after and after.raw_status}"
        )

    async def _cancel_again(self, order: ExchangeOrder) -> tuple[bool, str]:
        again = await self._trading.cancel_order(order.exchange_order_id, self._symbol)
        ok = again is not None and again.status is OrderStatus.CANCELED
        return ok, f"second cancel reported {again and again.raw_status}"

    async def _ioc_unfilled(self, market: Market) -> tuple[bool, str]:
        bid, _ = await self._touch()
        price = round_down_to_step(bid * RESTING_DISCOUNT, market.price_step)
        order = await self._submit(
            self._intent(Side.BUY, self._amount_for(market, price), price, TimeInForce.IOC)
        )
        ok = order.status is OrderStatus.EXPIRED and order.filled == 0
        return ok, f"{order.raw_status} filled={canonical_str(order.filled)}"

    async def _ioc_fill(
        self, market: Market, side: Side, amount: Decimal | None
    ) -> tuple[bool, str, ExchangeOrder]:
        bid, ask = await self._touch()
        if side is Side.BUY:
            price = round_up_to_step(ask * (1 + IOC_CROSS), market.price_step)
        else:
            price = round_down_to_step(bid * (1 - IOC_CROSS), market.price_step)
        size = amount if amount is not None else self._amount_for(market, price)
        submitted_at = self._clock.now()
        order = await self._submit(self._intent(side, size, price, TimeInForce.IOC))
        trades = await self._order_trades(order, submitted_at)
        traded = sum((t.amount for t in trades), Decimal(0))
        fees = sorted({f"{canonical_str(t.fee.amount)} {t.fee.currency}" for t in trades if t.fee})
        ok = order.filled > 0 and traded == order.filled
        return (
            ok,
            f"{order.raw_status} filled {canonical_str(order.filled)}/{canonical_str(size)} "
            f"avg {order.average_price}; {len(trades)} trade(s) sum "
            f"{canonical_str(traded)}; fees {', '.join(fees) or 'none'}",
            order,
        )

    async def _unknown_lookups(self) -> tuple[bool, str]:
        never_sent = self._intent(Side.BUY, Decimal(1), Decimal(1), TimeInForce.IOC)
        by_client = await self._trading.fetch_order_by_client_id(
            never_sent.client_order_id, self._symbol
        )
        by_id = await self._trading.fetch_order("1", self._symbol)
        return by_client is None and by_id is None, (
            f"unknown client id -> {by_client}; unknown order id -> {by_id}"
        )

    # --- helpers -----------------------------------------------------------------

    async def _order_trades(
        self, order: ExchangeOrder, submitted_at: datetime
    ) -> list[ExchangeTrade]:
        # Venue and local clocks differ by up to the allowed skew.
        trades = await self._trading.fetch_trades(
            self._symbol, since=submitted_at - timedelta(minutes=1)
        )
        return [t for t in trades if t.exchange_order_id == order.exchange_order_id]

    async def _touch(self) -> tuple[Decimal, Decimal]:
        ticker = await self._data.fetch_ticker(self._symbol)
        if ticker.bid is None or ticker.ask is None:
            msg = f"{self._symbol}: no bid/ask on the sandbox book"
            raise InvalidVenueData(msg)
        return ticker.bid, ticker.ask

    def _amount_for(self, market: Market, price: Decimal) -> Decimal:
        with localcontext(FINANCIAL_CONTEXT):
            target = max(market.min_cost * NOTIONAL_MULTIPLE, Decimal(1))
            amount = round_up_to_step(target / price, market.amount_step)
        return max(amount, market.min_amount)

    def _intent(self, side: Side, amount: Decimal, price: Decimal, tif: TimeInForce) -> OrderIntent:
        return OrderIntent(
            signal_id=uuid.uuid4(),
            strategy_id=STRATEGY_ID,
            exchange_id=self._trading.exchange_id,
            symbol=self._symbol,
            side=side,
            order_type=OrderType.LIMIT,
            amount=amount,
            limit_price=price,
            time_in_force=tif,
            created_at=self._clock.now(),
            sizing_policy=STRATEGY_ID,
        )

    async def _submit(self, intent: OrderIntent) -> ExchangeOrder:
        return await self._trading.submit_order(intent)

    async def _step[T](
        self,
        report: QualificationReport,
        name: str,
        run: Callable[[], Awaitable[tuple[bool, str] | tuple[bool, str, T]]],
    ) -> T | None:
        try:
            result = await run()
        except VenueError as exc:
            report.record(name, False, f"{type(exc).__name__}: {exc}")
            return None
        report.record(name, result[0], result[1])
        return result[2] if len(result) == 3 else None

    async def _cleanup(self, report: QualificationReport) -> None:
        for order_id in sorted(self._open):
            try:
                final = await self._trading.cancel_order(order_id, self._symbol)
                detail = f"{order_id}: {final and final.raw_status}"
                ok = final is not None and final.status is not OrderStatus.OPEN
            except VenueError as exc:
                detail, ok = f"{order_id}: cancel failed: {exc}", False
            report.record("cleanup", ok, detail)
        self._open.clear()
