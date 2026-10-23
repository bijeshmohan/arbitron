"""Contract tests: the authenticated Binance adapter over real CCXT parsing.

Payloads in tests/fixtures/binance/testnet/ were recorded from the Binance spot
testnet (docs/sandbox-qualification.md). Errors are raised as CCXT raises them
for the venue codes observed there.
"""

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

import ccxt
import ccxt.async_support as ccxt_async
import pytest

from arbitron.clocks import ManualClock
from arbitron.config.settings import ArbitronConfig
from arbitron.domain.enums import OrderStatus, OrderType, Side, TimeInForce
from arbitron.domain.orders import OrderIntent
from arbitron.exchange.ccxt_adapter import venue_options
from arbitron.exchange.ccxt_trading import CcxtTrading, create_trading
from arbitron.ports import errors as venue
from tests.contract.conftest import load_fixture
from tests.unit.test_config import example_data

S = OrderStatus
SYMBOL = "BTC/USDT"
NOW = datetime.fromtimestamp(1792774900, tz=UTC)

type Route = tuple[str, str]  # (method, path)


def recorded(name: str) -> Any:
    return load_fixture(f"testnet/{name}")


class VenueTransport:
    """Replaces ``Exchange.fetch``, routing on method and path; records requests."""

    def __init__(self) -> None:
        self.routes: dict[Route, Callable[[dict[str, str]], Any]] = {
            ("GET", "/api/v3/exchangeInfo"): lambda _: load_fixture("exchange_info.json"),
        }
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    def on(self, method: str, path: str, response: Any) -> None:
        def respond(_: dict[str, str]) -> Any:
            if isinstance(response, BaseException):
                raise response
            return response

        self.routes[(method, path)] = respond

    def calls(self, method: str, path: str) -> list[dict[str, str]]:
        return [q for m, p, q in self.requests if (m, p) == (method, path)]

    async def __call__(
        self, url: str, method: str = "GET", headers: Any = None, body: Any = None
    ) -> Any:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query)) | dict(parse_qsl(body or ""))
        self.requests.append((method, parts.path, query))
        handler = self.routes.get((method, parts.path))
        if handler is None:
            msg = f"unexpected venue request: {method} {url}"
            raise AssertionError(msg)
        return handler(query)


def sandbox_config(**app: Any) -> ArbitronConfig:
    data = example_data()
    data["app"].update({"mode": "sandbox", **app})
    data["exchange"]["sandbox"] = True
    return ArbitronConfig.model_validate(data)


def sandboxed_exchange() -> tuple[ccxt_async.Exchange, VenueTransport]:
    exchange = ccxt_async.binance(
        {
            "apiKey": "test-key",
            "secret": "test-secret",
            "enableRateLimit": False,
            "options": dict(venue_options("binance")),
        }
    )
    exchange.set_sandbox_mode(True)
    transport = VenueTransport()
    exchange.fetch = transport  # type: ignore[method-assign]
    return exchange, transport


@pytest.fixture
async def venue_pair() -> AsyncIterator[tuple[CcxtTrading, VenueTransport]]:
    exchange, transport = sandboxed_exchange()
    adapter = create_trading(sandbox_config(), ManualClock(NOW), exchange=exchange)
    yield adapter, transport
    await adapter.close()


def intent(
    *,
    amount: str = "0.00013",
    price: str = "81829.92",
    tif: TimeInForce = TimeInForce.IOC,
    side: Side = Side.BUY,
) -> OrderIntent:
    return OrderIntent(
        signal_id=uuid4(),
        strategy_id="sma_cross",
        exchange_id="binance",
        symbol=SYMBOL,
        side=side,
        order_type=OrderType.LIMIT,
        amount=Decimal(amount),
        limit_price=Decimal(price),
        time_in_force=tif,
        created_at=NOW,
        sizing_policy="fixed_notional_v1",
    )


class TestSubmit:
    async def test_filled_ioc(self, venue_pair: tuple[CcxtTrading, VenueTransport]) -> None:
        adapter, transport = venue_pair
        transport.on("POST", "/api/v3/order", recorded("order_ioc_filled.json"))
        sent = intent()

        order = await adapter.submit_order(sent)

        assert order.status is S.FILLED
        assert order.raw_status == "FILLED"
        assert order.exchange_order_id == "6243128"
        assert order.client_order_id == "arbcef081991cc74bf585ab6ae71ae01"
        assert order.amount == order.filled == Decimal("0.00013")
        assert order.remaining == 0
        assert order.average_price == Decimal("81748.17")
        assert order.fee is not None
        assert order.fee.currency == "BTC"
        (request,) = transport.calls("POST", "/api/v3/order")
        assert request["newClientOrderId"] == sent.client_order_id
        assert request["timeInForce"] == "IOC"
        assert request["type"] == "LIMIT"
        assert request["quantity"] == "0.00013"
        assert request["price"] == "81829.92"

    async def test_resting_gtc_is_open(
        self, venue_pair: tuple[CcxtTrading, VenueTransport]
    ) -> None:
        adapter, transport = venue_pair
        transport.on("POST", "/api/v3/order", recorded("order_gtc_new.json"))

        order = await adapter.submit_order(intent(price="73573.34", tif=TimeInForce.GTC))

        assert order.status is S.OPEN
        assert order.filled == 0
        assert order.average_price is None
        assert transport.calls("POST", "/api/v3/order")[0]["timeInForce"] == "GTC"

    async def test_unfilled_ioc_expires(
        self, venue_pair: tuple[CcxtTrading, VenueTransport]
    ) -> None:
        adapter, transport = venue_pair
        transport.on("POST", "/api/v3/order", recorded("order_ioc_expired.json"))

        order = await adapter.submit_order(intent(price="73573.34"))

        assert order.status is S.EXPIRED
        assert order.filled == 0

    @pytest.mark.parametrize(
        ("amount", "price"), [("0.000135", "81829.92"), ("0.00013", "81829.925")]
    )
    async def test_off_grid_values_are_refused_before_sending(
        self, venue_pair: tuple[CcxtTrading, VenueTransport], amount: str, price: str
    ) -> None:
        adapter, transport = venue_pair

        with pytest.raises(venue.InvalidOrder, match="step grid"):
            await adapter.submit_order(intent(amount=amount, price=price))

        assert not transport.calls("POST", "/api/v3/order")

    async def test_filter_failure_is_a_definitive_rejection(
        self, venue_pair: tuple[CcxtTrading, VenueTransport]
    ) -> None:
        adapter, transport = venue_pair
        transport.on(
            "POST",
            "/api/v3/order",
            ccxt.BadRequest('binance {"code":-1013,"msg":"Filter failure: NOTIONAL"}'),
        )

        with pytest.raises(venue.InvalidOrder, match="NOTIONAL"):
            await adapter.submit_order(intent(amount="0.00001"))

    async def test_duplicate_client_id_is_an_unknown_outcome(
        self, venue_pair: tuple[CcxtTrading, VenueTransport]
    ) -> None:
        adapter, transport = venue_pair
        transport.on(
            "POST",
            "/api/v3/order",
            ccxt.InvalidOrder('binance {"code":-2010,"msg":"Duplicate order sent."}'),
        )

        with pytest.raises(venue.UnknownExchangeError, match="duplicate client order id"):
            await adapter.submit_order(intent())

    async def test_insufficient_balance_stays_definitive(
        self, venue_pair: tuple[CcxtTrading, VenueTransport]
    ) -> None:
        adapter, transport = venue_pair
        transport.on(
            "POST",
            "/api/v3/order",
            ccxt.InsufficientFunds(
                'binance {"code":-2010,"msg":"Account has insufficient balance."}'
            ),
        )

        with pytest.raises(venue.InsufficientFunds):
            await adapter.submit_order(intent())

    async def test_submission_is_never_retried(
        self, venue_pair: tuple[CcxtTrading, VenueTransport]
    ) -> None:
        adapter, transport = venue_pair
        transport.on("POST", "/api/v3/order", ccxt.RequestTimeout("timed out"))

        with pytest.raises(venue.RequestTimeout):
            await adapter.submit_order(intent())

        assert len(transport.calls("POST", "/api/v3/order")) == 1

    async def test_symbol_outside_allowlist_is_refused(
        self, venue_pair: tuple[CcxtTrading, VenueTransport]
    ) -> None:
        adapter, _ = venue_pair
        foreign = intent()
        object.__setattr__(foreign, "symbol", "ETH/USDT")

        with pytest.raises(venue.MarketUnavailable):
            await adapter.submit_order(foreign)


class TestQueries:
    async def test_lookup_by_client_id(
        self, venue_pair: tuple[CcxtTrading, VenueTransport]
    ) -> None:
        adapter, transport = venue_pair
        transport.on("GET", "/api/v3/order", recorded("order_query_filled.json"))

        order = await adapter.fetch_order_by_client_id("arbcef081991cc74bf585ab6ae71ae01", SYMBOL)

        assert order is not None
        assert order.status is S.FILLED
        (request,) = transport.calls("GET", "/api/v3/order")
        assert request["origClientOrderId"] == "arbcef081991cc74bf585ab6ae71ae01"
        assert "orderId" not in request

    async def test_lookup_returning_another_client_id_is_invalid(
        self, venue_pair: tuple[CcxtTrading, VenueTransport]
    ) -> None:
        adapter, transport = venue_pair
        transport.on("GET", "/api/v3/order", recorded("order_query_filled.json"))

        with pytest.raises(venue.InvalidVenueData):
            await adapter.fetch_order_by_client_id("arb00000000000000000000000000000", SYMBOL)

    @pytest.mark.parametrize("by_client_id", [True, False])
    async def test_not_found_is_none(
        self, venue_pair: tuple[CcxtTrading, VenueTransport], by_client_id: bool
    ) -> None:
        adapter, transport = venue_pair
        transport.on(
            "GET",
            "/api/v3/order",
            ccxt.OrderNotFound('binance {"code":-2013,"msg":"Order does not exist."}'),
        )

        if by_client_id:
            found = await adapter.fetch_order_by_client_id("arb" + "0" * 29, SYMBOL)
        else:
            found = await adapter.fetch_order("1", SYMBOL)

        assert found is None

    async def test_open_orders_are_queried_per_symbol(
        self, venue_pair: tuple[CcxtTrading, VenueTransport]
    ) -> None:
        adapter, transport = venue_pair
        transport.on("GET", "/api/v3/openOrders", recorded("open_orders.json"))

        orders = await adapter.fetch_open_orders()

        assert [o.status for o in orders] == [S.OPEN]
        assert orders[0].client_order_id == "arb54359948d25d4526970f6639a0b85"
        assert [q["symbol"] for q in transport.calls("GET", "/api/v3/openOrders")] == ["BTCUSDT"]

    async def test_balances_are_exact(self, venue_pair: tuple[CcxtTrading, VenueTransport]) -> None:
        adapter, transport = venue_pair
        transport.on("GET", "/api/v3/account", recorded("account.json"))

        balances = await adapter.fetch_balances()

        assert set(balances) == {"BTC", "USDT", "ETH"}
        assert balances["USDT"].free == Decimal("9999.9999896")
        assert balances["BTC"].total == Decimal(1)
        assert balances["BTC"].observed_at == NOW


class TestCancel:
    async def test_cancel_reports_our_client_id(
        self, venue_pair: tuple[CcxtTrading, VenueTransport]
    ) -> None:
        adapter, transport = venue_pair
        transport.on("DELETE", "/api/v3/order", recorded("order_cancel.json"))

        order = await adapter.cancel_order("6243130", SYMBOL)

        assert order is not None
        assert order.status is S.CANCELED
        # The venue's response carries a new random clientOrderId; ours is the original.
        assert order.client_order_id == "arb54359948d25d4526970f6639a0b85"

    async def test_cancel_of_a_closed_order_reports_its_final_state(
        self, venue_pair: tuple[CcxtTrading, VenueTransport]
    ) -> None:
        adapter, transport = venue_pair
        transport.on(
            "DELETE",
            "/api/v3/order",
            ccxt.OrderNotFound('binance {"code":-2011,"msg":"Unknown order sent."}'),
        )
        transport.on("GET", "/api/v3/order", recorded("order_query_filled.json"))

        order = await adapter.cancel_order("6243128", SYMBOL)

        assert order is not None
        assert order.status is S.FILLED
        assert len(transport.calls("DELETE", "/api/v3/order")) == 1

    async def test_cancel_is_never_retried(
        self, venue_pair: tuple[CcxtTrading, VenueTransport]
    ) -> None:
        adapter, transport = venue_pair
        transport.on("DELETE", "/api/v3/order", ccxt.NetworkError("reset"))

        with pytest.raises(venue.NetworkTransient):
            await adapter.cancel_order("6243130", SYMBOL)

        assert len(transport.calls("DELETE", "/api/v3/order")) == 1


class TestTrades:
    async def test_trades_are_parsed_exactly(
        self, venue_pair: tuple[CcxtTrading, VenueTransport]
    ) -> None:
        adapter, transport = venue_pair
        transport.on("GET", "/api/v3/myTrades", recorded("my_trades.json"))

        trades = await adapter.fetch_trades(SYMBOL, since=NOW - timedelta(minutes=5))

        first = trades[0]
        assert first.trade_id == "1197049"
        assert first.exchange_order_id == "6242777"
        assert first.side is Side.BUY
        assert first.amount == Decimal("0.00013")
        assert first.price == Decimal("81709.68")
        assert first.fee is not None
        assert first.fee.currency == "BTC"
        assert first.fee.amount == 0
        assert [t.timestamp for t in trades] == sorted(t.timestamp for t in trades)

    async def test_long_ranges_are_split_into_venue_windows(
        self, venue_pair: tuple[CcxtTrading, VenueTransport]
    ) -> None:
        adapter, transport = venue_pair
        transport.on("GET", "/api/v3/myTrades", recorded("my_trades.json"))

        trades = await adapter.fetch_trades(SYMBOL, since=NOW - timedelta(hours=30))

        windows = transport.calls("GET", "/api/v3/myTrades")
        assert len(windows) == 2
        for window in windows:
            span = int(window["endTime"]) - int(window["startTime"])
            assert span < 24 * 3600 * 1000
        assert int(windows[1]["startTime"]) == int(windows[0]["endTime"]) + 1
        assert int(windows[-1]["endTime"]) == int(NOW.timestamp() * 1000)
        # The same trades returned for both windows collapse to one each.
        assert len(trades) == len(recorded("my_trades.json"))

    async def test_full_window_is_paged_from_its_last_trade(
        self, venue_pair: tuple[CcxtTrading, VenueTransport]
    ) -> None:
        adapter, transport = venue_pair
        start_ms = int((NOW - timedelta(hours=1)).timestamp() * 1000)
        template = recorded("my_trades.json")[0]

        def page(query: dict[str, str]) -> list[dict[str, Any]]:
            # 1000 trades one second apart from the window start, then the rest.
            begin = int(query["startTime"])
            first = begin == start_ms
            size = 1000 if first else 3
            base = 0 if first else 999  # the second page repeats the last trade
            return [
                template | {"id": 5000 + base + i, "time": start_ms + (base + i) * 1000}
                for i in range(size)
            ]

        transport.routes[("GET", "/api/v3/myTrades")] = page

        trades = await adapter.fetch_trades(SYMBOL, since=NOW - timedelta(hours=1))

        windows = transport.calls("GET", "/api/v3/myTrades")
        assert len(windows) == 2
        assert int(windows[1]["startTime"]) == start_ms + 999 * 1000
        assert len(trades) == 1002  # the repeated trade collapses
        assert len({t.trade_id for t in trades}) == 1002

    async def test_limit_caps_the_result(
        self, venue_pair: tuple[CcxtTrading, VenueTransport]
    ) -> None:
        adapter, transport = venue_pair
        transport.on("GET", "/api/v3/myTrades", recorded("my_trades.json"))

        trades = await adapter.fetch_trades(SYMBOL, since=NOW - timedelta(hours=30), limit=2)

        assert len(trades) == 2
        assert len(transport.calls("GET", "/api/v3/myTrades")) == 1


class TestSandboxOnly:
    def test_paper_mode_is_refused(self) -> None:
        exchange, _ = sandboxed_exchange()
        data = example_data()

        with pytest.raises(venue.UnsupportedCapability, match="sandbox mode"):
            create_trading(ArbitronConfig.model_validate(data), ManualClock(NOW), exchange=exchange)

    def test_production_endpoints_are_refused(self) -> None:
        exchange = ccxt_async.binance({"options": dict(venue_options("binance"))})

        with pytest.raises(venue.UnsupportedCapability, match="not pointed at its sandbox"):
            create_trading(sandbox_config(), ManualClock(NOW), exchange=exchange)

    def test_missing_credentials_are_refused(self) -> None:
        from arbitron.config.settings import Secrets  # noqa: PLC0415

        with pytest.raises(venue.AuthenticationFailure, match="must be set"):
            create_trading(
                sandbox_config(),
                ManualClock(NOW),
                secrets=Secrets(exchange_api_key=None, exchange_api_secret=None),
            )
