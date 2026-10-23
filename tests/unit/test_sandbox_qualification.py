"""Sandbox qualification logic against an in-memory venue with Binance semantics.

The real run is ``arbitron sandbox qualify`` / tests/sandbox; this checks that
each step passes on a correct venue and fails on a venue that misbehaves.
"""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import count

from arbitron.application.sandbox_qualification import QualificationReport, SandboxQualification
from arbitron.clocks import ManualClock
from arbitron.domain.enums import OrderStatus, Side, TimeInForce
from arbitron.domain.fills import ExchangeTrade, Fee
from arbitron.domain.market import Candle, Market, Ticker, Timeframe
from arbitron.domain.money import is_multiple_of_step
from arbitron.domain.orders import ExchangeOrder, OrderIntent
from arbitron.domain.portfolio import Balance
from arbitron.ports.errors import InvalidOrder, UnknownExchangeError
from tests.factories import T0, balance, make_intent, make_market, make_ticker

S = OrderStatus
SYMBOL = "BTC/USDT"


class FakeMarketData:
    def __init__(self, market: Market, ticker: Ticker, clock: ManualClock) -> None:
        self.market, self.ticker, self.clock = market, ticker, clock

    async def load_markets(self) -> Mapping[str, Market]:
        return {SYMBOL: self.market}

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Sequence[Candle]:
        return []

    async def fetch_ticker(self, symbol: str) -> Ticker:
        return self.ticker

    async def fetch_time(self) -> datetime:
        return self.clock.now()

    async def close(self) -> None:
        return None


class FakeVenue:
    """Enforces lot/tick/notional, rests GTC, fills marketable IOC, expires the rest."""

    exchange_id = "binance"

    def __init__(self, market: Market, ticker: Ticker, clock: ManualClock) -> None:
        self.market, self.ticker, self.clock = market, ticker, clock
        self.orders: dict[str, ExchangeOrder] = {}
        self.trades: list[ExchangeTrade] = []
        self.ids = count(1000)  # never the qualification's 'unknown order id' probe
        self.accept_duplicates = False
        self.cancel_keeps_open = False

    async def fetch_balances(self) -> Mapping[str, Balance]:
        return {"BTC": balance("BTC", "1"), "USDT": balance("USDT", "10000")}

    async def submit_order(self, intent: OrderIntent) -> ExchangeOrder:
        price = intent.limit_price
        assert price is not None
        if not is_multiple_of_step(intent.amount, self.market.amount_step):
            raise InvalidOrder("BTC/USDT amount is not on the venue's step grid")
        if intent.amount * price < self.market.min_cost:
            raise InvalidOrder('binance {"code":-1013,"msg":"Filter failure: NOTIONAL"}')
        duplicate = any(
            o.client_order_id == intent.client_order_id and o.status is S.OPEN
            for o in self.orders.values()
        )
        if duplicate and not self.accept_duplicates:
            raise UnknownExchangeError("duplicate client order id")
        assert self.ticker.bid is not None
        assert self.ticker.ask is not None
        marketable = (
            price >= self.ticker.ask if intent.side is Side.BUY else price <= self.ticker.bid
        )
        order_id = str(next(self.ids))
        if marketable:
            status, raw, filled = S.FILLED, "FILLED", intent.amount
            fill_price = self.ticker.ask if intent.side is Side.BUY else self.ticker.bid
            self.trades.append(
                ExchangeTrade(
                    "binance",
                    f"t{order_id}",
                    order_id,
                    SYMBOL,
                    intent.side,
                    intent.amount,
                    fill_price,
                    Fee(Decimal(0), "BTC"),
                    self.clock.now(),
                )
            )
        elif intent.time_in_force is TimeInForce.IOC:
            status, raw, filled = S.EXPIRED, "EXPIRED", Decimal(0)
        else:
            status, raw, filled = S.OPEN, "NEW", Decimal(0)
        order = ExchangeOrder(
            exchange_id="binance",
            exchange_order_id=order_id,
            client_order_id=intent.client_order_id,
            symbol=SYMBOL,
            side=intent.side,
            order_type=intent.order_type,
            status=status,
            raw_status=raw,
            amount=intent.amount,
            filled=filled,
            remaining=intent.amount - filled,
            average_price=price if filled else None,
            fee=None,
            timestamp=self.clock.now(),
        )
        self.orders[order_id] = order
        return order

    async def cancel_order(self, exchange_order_id: str, symbol: str) -> ExchangeOrder | None:
        order = self.orders.get(exchange_order_id)
        if order is None or order.status is not S.OPEN or self.cancel_keeps_open:
            return order
        canceled = replace(order, status=S.CANCELED, raw_status="CANCELED")
        self.orders[exchange_order_id] = canceled
        return canceled

    async def fetch_order(self, exchange_order_id: str, symbol: str) -> ExchangeOrder | None:
        return self.orders.get(exchange_order_id)

    async def fetch_order_by_client_id(
        self, client_order_id: str, symbol: str
    ) -> ExchangeOrder | None:
        return next((o for o in self.orders.values() if o.client_order_id == client_order_id), None)

    async def fetch_open_orders(self, symbol: str | None = None) -> Sequence[ExchangeOrder]:
        return [o for o in self.orders.values() if o.status is S.OPEN]

    async def fetch_trades(
        self, symbol: str, *, since: datetime | None = None, limit: int | None = None
    ) -> Sequence[ExchangeTrade]:
        return [t for t in self.trades if since is None or t.timestamp >= since]

    async def close(self) -> None:
        return None


def qualification(
    venue: FakeVenue | None = None,
) -> tuple[SandboxQualification, FakeVenue]:
    clock = ManualClock(T0)
    market, ticker = make_market(), make_ticker(timestamp=T0)
    venue = venue or FakeVenue(market, ticker, clock)
    return (
        SandboxQualification(
            market_data=FakeMarketData(market, ticker, clock),
            trading=venue,
            clock=clock,
            symbol=SYMBOL,
            max_clock_skew=timedelta(seconds=1),
        ),
        venue,
    )


def failed(report: QualificationReport) -> list[str]:
    return [r.name for r in report.results if not r.ok]


async def test_read_only_check_places_nothing() -> None:
    q, venue = qualification()

    report = await q.check()

    assert report.ok
    assert [r.name for r in report.results] == ["clock", "market", "balances", "open orders"]
    assert not venue.orders


async def test_lifecycle_passes_on_a_correct_venue_and_leaves_nothing_open() -> None:
    q, venue = qualification()

    report = await q.order_lifecycle()

    assert failed(report) == []
    assert not await venue.fetch_open_orders()
    assert {o.status for o in venue.orders.values()} == {S.CANCELED, S.EXPIRED, S.FILLED}


async def test_accepted_duplicate_fails_and_is_cleaned_up() -> None:
    q, venue = qualification()
    venue.accept_duplicates = True

    report = await q.order_lifecycle()

    assert failed(report) == ["duplicate client id"]
    assert not await venue.fetch_open_orders()


async def test_cancel_that_does_not_cancel_fails() -> None:
    q, venue = qualification()
    venue.cancel_keeps_open = True

    report = await q.order_lifecycle()

    assert "cancel" in failed(report)
    assert "cleanup" in failed(report)  # the order is still open, and the report says so


async def test_lifecycle_is_skipped_when_orders_are_already_open() -> None:
    q, venue = qualification()
    await venue.submit_order(
        make_intent(
            amount=Decimal("0.1"), limit_price=Decimal("90.00"), time_in_force=TimeInForce.GTC
        )
    )

    report = await q.order_lifecycle()

    assert failed(report) == ["open orders", "lifecycle"]
    assert len(venue.orders) == 1
