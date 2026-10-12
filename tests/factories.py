"""Builders for valid domain objects; tests override only what they exercise."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from arbitron.domain.enums import KillSwitchState, MarketType, OrderStatus, OrderType, Side
from arbitron.domain.fills import ExchangeTrade, Fee
from arbitron.domain.market import Candle, Market, Ticker, Timeframe
from arbitron.domain.orders import Order, OrderIntent
from arbitron.domain.portfolio import Balance
from arbitron.domain.risk import KillSwitch
from arbitron.domain.signals import Signal
from arbitron.domain.snapshot import TradingSnapshot
from arbitron.domain.strategies import StrategyConfig

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
FIVE_MIN = Timeframe("5m")
SMA_CONFIG = StrategyConfig("sma_cross", "1", {"fast": 10, "slow": 30})


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def make_signal(**overrides: Any) -> Signal:
    fields: dict[str, Any] = {
        "strategy_id": "sma_cross",
        "strategy_version": "1",
        "config_hash": SMA_CONFIG.config_hash,
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


# --- Sizing and risk -------------------------------------------------------
# Round numbers so expected sizes can be checked by hand: price ~100 USDT,
# lot step 0.001, tick 0.01, minimum notional 5 USDT.


def make_market(**overrides: Any) -> Market:
    fields: dict[str, Any] = {
        "exchange_id": "binance",
        "symbol": "BTC/USDT",
        "base": "BTC",
        "quote": "USDT",
        "market_type": MarketType.SPOT,
        "amount_step": Decimal("0.001"),
        "price_step": Decimal("0.01"),
        "min_amount": Decimal("0.001"),
        "min_cost": Decimal(5),
        "active": True,
    }
    fields.update(overrides)
    return Market(**fields)


def make_ticker(bid: str = "100.00", ask: str = "100.01", **overrides: Any) -> Ticker:
    fields: dict[str, Any] = {
        "exchange_id": "binance",
        "symbol": "BTC/USDT",
        "bid": Decimal(bid),
        "ask": Decimal(ask),
        "last": Decimal(ask),
        "timestamp": T0,
    }
    fields.update(overrides)
    return Ticker(**fields)


def balance(asset: str, free: str, used: str = "0") -> Balance:
    return Balance(asset, Decimal(free), Decimal(used), Decimal(free) + Decimal(used), T0)


ARMED = KillSwitch(KillSwitchState.ARMED, T0, "alice", "armed for tests")


def make_snapshot(**overrides: Any) -> TradingSnapshot:
    """A healthy, flat account with 1000 USDT: anything sized sensibly is approved."""
    fields: dict[str, Any] = {
        "as_of": T0,
        "quote_currency": "USDT",
        "markets": {"BTC/USDT": make_market()},
        "marks": {"BTC/USDT": Decimal(100)},
        "tickers": {"BTC/USDT": make_ticker()},
        "balances": {"USDT": balance("USDT", "1000")},
        "open_orders": (),
        "orders_last_hour": 0,
        "day_start_equity": Decimal(1000),
        "peak_equity": Decimal(1000),
        "market_data_ok": {"BTC/USDT": True},
        "kill_switch": ARMED,
        "trading_enabled": True,
        "system_ready": True,
        "reconciliation_clean": True,
        "exchange_healthy": True,
    }
    fields.update(overrides)
    return TradingSnapshot(**fields)
