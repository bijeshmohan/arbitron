"""Recording a risk decision against a created order (plan §12.1 steps 3-4).

The decision and the order's transition to ``RISK_APPROVED`` / ``RISK_REJECTED``
are written in the caller's unit of work, so they commit together. The
database independently refuses a ``risk_approved`` order without an approved
decision row (migration trigger), so no code path can approve without one.
"""

from arbitron.domain.enums import OrderStatus
from arbitron.domain.events import OrderEvent
from arbitron.domain.orders import Order
from arbitron.domain.risk import RiskDecision
from arbitron.ports.repositories import UnitOfWork


async def apply_risk_decision(
    tx: UnitOfWork, order: Order, decision: RiskDecision, *, correlation_id: str
) -> Order:
    """Persist ``decision`` and move ``order`` out of ``CREATED``. The caller commits."""
    if decision.intent_id != order.id:
        msg = f"decision for {decision.intent_id} applied to order {order.id}"
        raise ValueError(msg)
    if order.status is not OrderStatus.CREATED:
        msg = f"order {order.id} is {order.status}; risk is decided only for created orders"
        raise ValueError(msg)
    target = OrderStatus.RISK_APPROVED if decision.approved else OrderStatus.RISK_REJECTED
    updated = order.transition(target, at=max(decision.decided_at, order.updated_at))
    await tx.risk_decisions.add(decision)
    await tx.orders.apply_transition(
        order,
        updated,
        OrderEvent(
            order_id=order.id,
            from_status=order.status,
            to_status=target,
            occurred_at=updated.updated_at,
            correlation_id=correlation_id,
            payload={
                "policy_version": decision.policy_version,
                "reasons": ",".join(r.value for r in decision.reasons),
            },
        ),
    )
    return updated
