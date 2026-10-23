"""Venue order/trade/balance payloads -> domain (exchange.mapping)."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from arbitron.domain.enums import OrderStatus
from arbitron.exchange.mapping import (
    balances_from_ccxt,
    exchange_order_from_ccxt,
    order_status_from_venue,
)
from arbitron.ports.errors import InvalidVenueData

S = OrderStatus
NOW = datetime(2026, 10, 23, tzinfo=UTC)
ZERO, HALF, ONE = Decimal(0), Decimal("0.5"), Decimal(1)


@pytest.mark.parametrize(
    ("raw", "filled", "expected"),
    [
        ("NEW", ZERO, S.OPEN),
        ("PARTIALLY_FILLED", HALF, S.PARTIALLY_FILLED),
        ("PENDING_CANCEL", ZERO, S.OPEN),
        ("PENDING_CANCEL", HALF, S.PARTIALLY_FILLED),
        ("FILLED", ONE, S.FILLED),
        ("CANCELED", ZERO, S.CANCELED),
        ("CANCELED", HALF, S.PARTIALLY_FILLED_CLOSED),
        ("EXPIRED", ZERO, S.EXPIRED),
        ("EXPIRED", HALF, S.PARTIALLY_FILLED_CLOSED),
        ("EXPIRED_IN_MATCH", ZERO, S.EXPIRED),
        ("EXPIRED_IN_MATCH", HALF, S.PARTIALLY_FILLED_CLOSED),
        ("REJECTED", ZERO, S.EXCHANGE_REJECTED),
    ],
)
def test_status_mapping(raw: str, filled: Decimal, expected: OrderStatus) -> None:
    assert order_status_from_venue(raw, filled=filled, amount=ONE) is expected


def test_unknown_status_is_invalid() -> None:
    with pytest.raises(InvalidVenueData, match="unknown venue order status"):
        order_status_from_venue("TRIGGERED", filled=ZERO, amount=ONE)


def ccxt_order(**overrides: Any) -> dict[str, Any]:
    order: dict[str, Any] = {
        "id": "1",
        "clientOrderId": "arb" + "0" * 29,
        "symbol": "BTC/USDT",
        "side": "buy",
        "type": "limit",
        "status": "closed",
        "amount": 0.002,
        "filled": 0.002,
        "average": 100.5,
        "timestamp": 1792774881143,
        "info": {"status": "FILLED"},
        "fees": [],
    }
    order.update(overrides)
    return order


class TestOrder:
    def test_fees_in_one_currency_are_summed(self) -> None:
        order = exchange_order_from_ccxt(
            ccxt_order(
                fees=[{"cost": 0.000001, "currency": "BTC"}, {"cost": 0.000002, "currency": "BTC"}]
            ),
            exchange_id="binance",
        )

        assert order.fee is not None
        assert order.fee.amount == Decimal("0.000003")

    def test_fees_in_several_currencies_are_left_to_the_trades(self) -> None:
        order = exchange_order_from_ccxt(
            ccxt_order(fees=[{"cost": 0.1, "currency": "BNB"}, {"cost": 0.2, "currency": "BTC"}]),
            exchange_id="binance",
        )

        assert order.fee is None

    def test_unsupported_order_type_is_invalid(self) -> None:
        with pytest.raises(InvalidVenueData, match="order 1"):
            exchange_order_from_ccxt(ccxt_order(type="stop_loss"), exchange_id="binance")

    def test_unfilled_order_has_no_average_price(self) -> None:
        order = exchange_order_from_ccxt(
            ccxt_order(filled=0, average=None, info={"status": "NEW"}), exchange_id="binance"
        )

        assert order.status is S.OPEN
        assert order.average_price is None
        assert order.remaining == Decimal("0.002")


class TestBalances:
    def test_zero_balances_are_dropped_and_totals_exact(self) -> None:
        balances = balances_from_ccxt(
            {"free": {"BTC": 0.1, "ETH": 0.0}, "used": {"BTC": 0.2, "ETH": 0.0}},
            observed_at=NOW,
        )

        assert set(balances) == {"BTC"}
        assert balances["BTC"].total == Decimal("0.3")  # not 0.30000000000000004

    def test_missing_maps_are_invalid(self) -> None:
        with pytest.raises(InvalidVenueData):
            balances_from_ccxt({"free": {}}, observed_at=NOW)

    def test_negative_balance_is_invalid(self) -> None:
        with pytest.raises(InvalidVenueData, match="BTC"):
            balances_from_ccxt({"free": {"BTC": -1.0}, "used": {}}, observed_at=NOW)
