"""Domain objects -> JSON. Decimals are exact strings; times are ISO-8601 UTC.

Only fields an operator needs are exposed: no credentials, no raw venue
payloads (plan §16).
"""

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal

from arbitron.domain.events import SystemEvent
from arbitron.domain.money import canonical_str
from arbitron.domain.orders import Order
from arbitron.domain.portfolio import Balance, EquitySnapshot
from arbitron.domain.positions import Position
from arbitron.domain.risk import KillSwitch, RiskDecision
from arbitron.domain.signals import Signal
from arbitron.execution.reconciler import ReconciliationReport

type Json = str | int | float | bool | list["Json"] | dict[str, "Json"] | None


def _d(value: Decimal | None) -> str | None:
    return canonical_str(value) if value is not None else None


def _t(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def kill_switch(state: KillSwitch | None) -> dict[str, Json]:
    if state is None:
        return {"state": "uninitialized"}
    return {
        "state": state.state.value,
        "changed_at": _t(state.changed_at),
        "actor": state.actor,
        "reason": state.reason,
        "allows_new_orders": state.allows_new_orders,
    }


def equity(snapshot: EquitySnapshot | None, peak: Decimal | None) -> dict[str, Json]:
    if snapshot is None:
        return {"latest": None, "peak": _d(peak)}
    return {
        "latest": _d(snapshot.equity),
        "quote_currency": snapshot.quote_currency,
        "taken_at": _t(snapshot.taken_at),
        "peak": _d(peak),
        "unvalued_assets": list(snapshot.unvalued_assets),
    }


def balance(b: Balance) -> dict[str, Json]:
    return {"free": _d(b.free), "used": _d(b.used), "total": _d(b.total)}


def balances(items: Mapping[str, Balance]) -> dict[str, Json]:
    return {asset: balance(b) for asset, b in sorted(items.items())}


def position(p: Position) -> dict[str, Json]:
    return {
        "symbol": p.symbol,
        "quantity": _d(p.quantity),
        "average_cost": _d(p.average_cost),
        "realized_pnl": _d(p.realized_pnl),
        "fees_paid": _d(p.fees_paid),
        "updated_at": _t(p.updated_at),
    }


def order(o: Order) -> dict[str, Json]:
    i = o.intent
    return {
        "id": str(o.id),
        "symbol": i.symbol,
        "side": i.side.value,
        "type": i.order_type.value,
        "time_in_force": i.time_in_force.value if i.time_in_force else None,
        "amount": _d(i.amount),
        "limit_price": _d(i.limit_price),
        "status": o.status.value,
        "filled": _d(o.filled_amount),
        "average_fill_price": _d(o.average_fill_price),
        "created_at": _t(i.created_at),
        "updated_at": _t(o.updated_at),
        "strategy_id": i.strategy_id,
        "signal_id": str(i.signal_id),
    }


def signal(s: Signal) -> dict[str, Json]:
    return {
        "id": str(s.id),
        "strategy": f"{s.strategy_id} v{s.strategy_version}",
        "symbol": s.symbol,
        "timeframe": s.timeframe.value,
        "side": s.side.value,
        "candle_time": _t(s.source_candle_time),
        "created_at": _t(s.created_at),
        "reason_code": s.reason_code,
        "explanation": s.explanation,
    }


def risk_decision(r: RiskDecision) -> dict[str, Json]:
    return {
        "intent_id": str(r.intent_id),
        "approved": r.approved,
        "reasons": [reason.value for reason in r.reasons],
        "policy_version": r.policy_version,
        "decided_at": _t(r.decided_at),
        "observations": dict(r.observations),
    }


def system_event(e: SystemEvent) -> dict[str, Json]:
    return {
        "type": e.event_type,
        "severity": e.severity.value,
        "component": e.component,
        "occurred_at": _t(e.occurred_at),
        "correlation_id": e.correlation_id,
        "payload": dict(e.payload),
    }


def reconciliation(report: ReconciliationReport) -> dict[str, Json]:
    return {
        "started_at": _t(report.started_at),
        "clean": report.clean,
        "resolved": list(report.resolved),
        "repairs": list(report.repairs),
        "pending": list(report.pending),
        "discrepancies": list(report.discrepancies),
    }
