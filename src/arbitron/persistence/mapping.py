"""Row <-> domain conversions. Every read re-validates through domain constructors."""

from datetime import datetime
from typing import Any

from arbitron.domain.enums import (
    KillSwitchState,
    MarketType,
    OrderStatus,
    OrderType,
    Severity,
    Side,
    TimeInForce,
)
from arbitron.domain.errors import DomainValidationError
from arbitron.domain.events import OrderEvent, SystemEvent
from arbitron.domain.experiments import ExperimentRecord
from arbitron.domain.fills import ExchangeTrade, Fee, Fill
from arbitron.domain.market import Candle, Market, Timeframe
from arbitron.domain.orders import Order, OrderIntent
from arbitron.domain.portfolio import EquitySnapshot
from arbitron.domain.positions import Position
from arbitron.domain.risk import KillSwitch, RiskDecision, RiskReason
from arbitron.domain.signals import Signal
from arbitron.domain.strategies import StrategyConfig, StrategyCursor
from arbitron.persistence.tables import (
    BacktestRunRow,
    CandleRow,
    EquitySnapshotRow,
    FillRow,
    KillSwitchRow,
    MarketRow,
    OrderEventRow,
    OrderRow,
    PositionRow,
    RiskDecisionRow,
    SignalRow,
    StrategyConfigRow,
    StrategyCursorRow,
    SystemEventRow,
)


def market_values(market: Market, observed_at: datetime) -> dict[str, Any]:
    return {
        "exchange_id": market.exchange_id,
        "symbol": market.symbol,
        "base": market.base,
        "quote": market.quote,
        "market_type": market.market_type.value,
        "amount_step": market.amount_step,
        "price_step": market.price_step,
        "min_amount": market.min_amount,
        "min_cost": market.min_cost,
        "active": market.active,
        "observed_at": observed_at,
    }


def market_from_row(row: MarketRow) -> Market:
    return Market(
        exchange_id=row.exchange_id,
        symbol=row.symbol,
        base=row.base,
        quote=row.quote,
        market_type=MarketType(row.market_type),
        amount_step=row.amount_step,
        price_step=row.price_step,
        min_amount=row.min_amount,
        min_cost=row.min_cost,
        active=row.active,
    )


def experiment_values(record: ExperimentRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "created_at": record.created_at,
        "label": record.label,
        "exchange_id": record.exchange_id,
        "symbols": list(record.symbols),
        "timeframe": record.timeframe,
        "start": record.start,
        "end": record.end,
        "strategy_id": record.strategy_id,
        "strategy_version": record.strategy_version,
        "config_hash": record.config_hash,
        "risk_policy_version": record.risk_policy_version,
        "code_version": record.code_version,
        "data_fingerprint": record.data_fingerprint,
        "candle_count": record.candle_count,
        "assumptions": dict(record.assumptions),
        "parameters": dict(record.parameters),
        "metrics": dict(record.metrics),
    }


def experiment_from_row(row: BacktestRunRow) -> ExperimentRecord:
    return ExperimentRecord(
        id=row.id,
        created_at=row.created_at,
        label=row.label,
        exchange_id=row.exchange_id,
        symbols=tuple(row.symbols),
        timeframe=row.timeframe,
        start=row.start,
        end=row.end,
        strategy_id=row.strategy_id,
        strategy_version=row.strategy_version,
        config_hash=row.config_hash,
        risk_policy_version=row.risk_policy_version,
        code_version=row.code_version,
        data_fingerprint=row.data_fingerprint,
        candle_count=row.candle_count,
        assumptions=row.assumptions,
        parameters=row.parameters,
        metrics=row.metrics,
    )


def candle_values(candle: Candle) -> dict[str, Any]:
    return {
        "exchange_id": candle.exchange_id,
        "symbol": candle.symbol,
        "timeframe": candle.timeframe.value,
        "open_time": candle.open_time,
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
        "is_closed": candle.is_closed,
        "ingested_at": candle.ingested_at,
    }


def candle_from_row(row: CandleRow) -> Candle:
    return Candle(
        exchange_id=row.exchange_id,
        symbol=row.symbol,
        timeframe=Timeframe(row.timeframe),
        open_time=row.open_time,
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        volume=row.volume,
        is_closed=row.is_closed,
        ingested_at=row.ingested_at,
    )


def signal_values(signal: Signal) -> dict[str, Any]:
    return {
        "id": signal.id,
        "dedupe_key": signal.dedupe_key,
        "strategy_id": signal.strategy_id,
        "strategy_version": signal.strategy_version,
        "config_hash": signal.config_hash,
        "exchange_id": signal.exchange_id,
        "symbol": signal.symbol,
        "timeframe": signal.timeframe.value,
        "side": signal.side.value,
        "source_candle_time": signal.source_candle_time,
        "created_at": signal.created_at,
        "reason_code": signal.reason_code,
        "explanation": signal.explanation,
        "metadata_": dict(signal.metadata),
    }


def signal_from_row(row: SignalRow) -> Signal:
    signal = Signal(
        strategy_id=row.strategy_id,
        strategy_version=row.strategy_version,
        config_hash=row.config_hash,
        exchange_id=row.exchange_id,
        symbol=row.symbol,
        timeframe=Timeframe(row.timeframe),
        side=Side(row.side),
        source_candle_time=row.source_candle_time,
        created_at=row.created_at,
        reason_code=row.reason_code,
        explanation=row.explanation,
        metadata=row.metadata_,
    )
    _require_same("signal id", row.id, signal.id)
    return signal


def strategy_config_from_row(row: StrategyConfigRow) -> StrategyConfig:
    config = StrategyConfig(row.strategy_id, row.version, row.parameters)
    _require_same("strategy config hash", row.config_hash, config.config_hash)
    return config


def strategy_cursor_from_row(row: StrategyCursorRow) -> StrategyCursor:
    return StrategyCursor(
        config_hash=row.config_hash,
        exchange_id=row.exchange_id,
        symbol=row.symbol,
        timeframe=Timeframe(row.timeframe),
        last_open_time=row.last_open_time,
        updated_at=row.updated_at,
    )


def order_values(order: Order) -> dict[str, Any]:
    intent = order.intent
    return {
        "id": order.id,
        "dedupe_key": intent.dedupe_key,
        "signal_id": intent.signal_id,
        "strategy_id": intent.strategy_id,
        "exchange_id": intent.exchange_id,
        "symbol": intent.symbol,
        "side": intent.side.value,
        "order_type": intent.order_type.value,
        "amount": intent.amount,
        "limit_price": intent.limit_price,
        "time_in_force": intent.time_in_force.value if intent.time_in_force else None,
        "intent_created_at": intent.created_at,
        "sizing_policy": intent.sizing_policy,
        "sizing_inputs": dict(intent.sizing_inputs),
        "attempt": intent.attempt,
        "client_order_id": intent.client_order_id,
        **order_state_values(order),
    }


def order_state_values(order: Order) -> dict[str, Any]:
    return {
        "status": order.status.value,
        "exchange_order_id": order.exchange_order_id,
        "filled_amount": order.filled_amount,
        "average_fill_price": order.average_fill_price,
        "updated_at": order.updated_at,
    }


def order_from_row(row: OrderRow) -> Order:
    intent = OrderIntent(
        signal_id=row.signal_id,
        strategy_id=row.strategy_id,
        exchange_id=row.exchange_id,
        symbol=row.symbol,
        side=Side(row.side),
        order_type=OrderType(row.order_type),
        amount=row.amount,
        limit_price=row.limit_price,
        time_in_force=TimeInForce(row.time_in_force) if row.time_in_force else None,
        created_at=row.intent_created_at,
        sizing_policy=row.sizing_policy,
        sizing_inputs=row.sizing_inputs,
        attempt=row.attempt,
    )
    # Identity is derived; a mismatch means the row was altered or corrupted.
    _require_same("order id", row.id, intent.id)
    _require_same("client order id", row.client_order_id, intent.client_order_id)
    return Order(
        intent=intent,
        status=OrderStatus(row.status),
        updated_at=row.updated_at,
        exchange_order_id=row.exchange_order_id,
        filled_amount=row.filled_amount,
        average_fill_price=row.average_fill_price,
    )


def order_event_values(event: OrderEvent) -> dict[str, Any]:
    return {
        "order_id": event.order_id,
        "from_status": event.from_status.value if event.from_status else None,
        "to_status": event.to_status.value,
        "occurred_at": event.occurred_at,
        "correlation_id": event.correlation_id,
        "payload": dict(event.payload),
    }


def order_event_from_row(row: OrderEventRow) -> OrderEvent:
    return OrderEvent(
        order_id=row.order_id,
        from_status=OrderStatus(row.from_status) if row.from_status else None,
        to_status=OrderStatus(row.to_status),
        occurred_at=row.occurred_at,
        correlation_id=row.correlation_id,
        payload=row.payload,
    )


def fill_values(fill: Fill) -> dict[str, Any]:
    trade = fill.trade
    return {
        "dedupe_key": fill.dedupe_key,
        "order_id": fill.order_id,
        "exchange_id": trade.exchange_id,
        "trade_id": trade.trade_id,
        "exchange_order_id": trade.exchange_order_id,
        "symbol": trade.symbol,
        "side": trade.side.value,
        "amount": trade.amount,
        "price": trade.price,
        "fee_amount": trade.fee.amount if trade.fee else None,
        "fee_currency": trade.fee.currency if trade.fee else None,
        "timestamp": trade.timestamp,
    }


def fill_from_row(row: FillRow) -> Fill:
    fee = None
    if row.fee_amount is not None and row.fee_currency is not None:
        fee = Fee(row.fee_amount, row.fee_currency)
    trade = ExchangeTrade(
        exchange_id=row.exchange_id,
        trade_id=row.trade_id,
        exchange_order_id=row.exchange_order_id,
        symbol=row.symbol,
        side=Side(row.side),
        amount=row.amount,
        price=row.price,
        fee=fee,
        timestamp=row.timestamp,
    )
    return Fill(order_id=row.order_id, trade=trade)


def risk_decision_values(decision: RiskDecision) -> dict[str, Any]:
    return {
        "intent_id": decision.intent_id,
        "approved": decision.approved,
        "reasons": [reason.value for reason in decision.reasons],
        "policy_version": decision.policy_version,
        "decided_at": decision.decided_at,
        "observations": dict(decision.observations),
    }


def risk_decision_from_row(row: RiskDecisionRow) -> RiskDecision:
    return RiskDecision(
        intent_id=row.intent_id,
        reasons=tuple(RiskReason(reason) for reason in row.reasons),
        policy_version=row.policy_version,
        decided_at=row.decided_at,
        observations=row.observations,
    )


def kill_switch_from_row(row: KillSwitchRow) -> KillSwitch:
    return KillSwitch(
        state=KillSwitchState(row.state),
        changed_at=row.changed_at,
        actor=row.actor,
        reason=row.reason,
    )


def system_event_values(event: SystemEvent) -> dict[str, Any]:
    return {
        "event_type": event.event_type,
        "severity": event.severity.value,
        "component": event.component,
        "occurred_at": event.occurred_at,
        "correlation_id": event.correlation_id,
        "payload": dict(event.payload),
    }


def system_event_from_row(row: SystemEventRow) -> SystemEvent:
    return SystemEvent(
        event_type=row.event_type,
        severity=Severity(row.severity),
        component=row.component,
        occurred_at=row.occurred_at,
        correlation_id=row.correlation_id,
        payload=row.payload,
    )


def position_values(position: Position) -> dict[str, Any]:
    return {
        "exchange_id": position.exchange_id,
        "symbol": position.symbol,
        "quantity": position.quantity,
        "average_cost": position.average_cost,
        "realized_pnl": position.realized_pnl,
        "fees_paid": position.fees_paid,
        "updated_at": position.updated_at,
    }


def position_from_row(row: PositionRow) -> Position:
    return Position(
        exchange_id=row.exchange_id,
        symbol=row.symbol,
        quantity=row.quantity,
        average_cost=row.average_cost,
        realized_pnl=row.realized_pnl,
        updated_at=row.updated_at,
        fees_paid=row.fees_paid,
    )


def equity_snapshot_values(snapshot: EquitySnapshot) -> dict[str, Any]:
    return {
        "taken_at": snapshot.taken_at,
        "quote_currency": snapshot.quote_currency,
        "equity": snapshot.equity,
        "components": dict(snapshot.components),
        "unvalued_assets": list(snapshot.unvalued_assets),
    }


def equity_snapshot_from_row(row: EquitySnapshotRow) -> EquitySnapshot:
    return EquitySnapshot(
        taken_at=row.taken_at,
        quote_currency=row.quote_currency,
        equity=row.equity,
        components=row.components,
        unvalued_assets=tuple(row.unvalued_assets),
    )


def _require_same(name: str, stored: object, derived: object) -> None:
    if stored != derived:
        msg = f"stored {name} {stored!r} does not match the value derived from its row {derived!r}"
        raise DomainValidationError(msg)
