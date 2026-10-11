"""Builders for valid domain objects; tests override only what they exercise."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from arbitron.domain.enums import OrderStatus, OrderType, Side
from arbitron.domain.fills import ExchangeTrade, Fee
from arbitron.domain.market import Candle, Timeframe
from arbitron.domain.orders import Order, OrderIntent
from arbitron.domain.signals import Signal

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
FIVE_MIN = Timeframe("5m")


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def make_signal(**overrides: Any) -> Signal:
    fields: dict[str, Any] = {
        "strategy_id": "sma_cross",
        "strategy_version": "1",
        "exchange_id": "binance",
        "symbol": "BTC/USDT",
        "timeframe": FIVE_MIN,
        "side": Side.BUY,
        "source_candle_time": T0,
        "created_at": at(5),
        "reason_code": "fast_crossed_above_slow",
    }
    fields.update(overrides)
    return Signal(**fields)


def make_intent(**overrides: Any) -> OrderIntent:
    fields: dict[str, Any] = {
        "signal_id": make_signal().id,
        "strategy_id": "sma_cross",
        "exchange_id": "binance",
        "symbol": "BTC/USDT",
        "side": Side.BUY,
        "order_type": OrderType.LIMIT,
        "amount": Decimal("0.010"),
        "limit_price": Decimal("50000.00"),
        "time_in_force": None,
        "created_at": at(5),
        "sizing_policy": "fixed_notional_v1",
    }
    fields.update(overrides)
    return OrderIntent(**fields)


def make_order(status: OrderStatus = OrderStatus.CREATED, **overrides: Any) -> Order:
    """An order directly in ``status`` with fill fields consistent with it."""
    intent = make_intent()
    filled = Decimal(0)
    if status in {OrderStatus.PARTIALLY_FILLED, OrderStatus.PARTIALLY_FILLED_CLOSED}:
        filled = Decimal("0.004")
    elif status is OrderStatus.FILLED:
        filled = intent.amount
    fields: dict[str, Any] = {
        "intent": intent,
        "status": status,
        "updated_at": at(6),
        "filled_amount": filled,
        "average_fill_price": Decimal("49990.00") if filled else None,
        "exchange_order_id": None
        if status
        in {
            OrderStatus.CREATED,
            OrderStatus.RISK_APPROVED,
            OrderStatus.RISK_REJECTED,
            OrderStatus.SUBMITTING,
        }
        else "ex-1",
    }
    fields.update(overrides)
    return replace(Order.new(intent), **fields)


def make_candle(**overrides: Any) -> Candle:
    fields: dict[str, Any] = {
        "exchange_id": "binance",
        "symbol": "BTC/USDT",
        "timeframe": FIVE_MIN,
        "open_time": T0,
        "open": Decimal(100),
        "high": Decimal(110),
        "low": Decimal(95),
        "close": Decimal(105),
        "volume": Decimal(3),
        "is_closed": True,
        "ingested_at": at(5),
    }
    fields.update(overrides)
    return Candle(**fields)


def make_trade(**overrides: Any) -> ExchangeTrade:
    fields: dict[str, Any] = {
        "exchange_id": "binance",
        "trade_id": "t-1",
        "exchange_order_id": "ex-1",
        "symbol": "BTC/USDT",
        "side": Side.BUY,
        "amount": Decimal("0.004"),
        "price": Decimal("49990"),
        "fee": Fee(Decimal("0.000004"), "BTC"),
        "timestamp": at(7),
    }
    fields.update(overrides)
    return ExchangeTrade(**fields)
