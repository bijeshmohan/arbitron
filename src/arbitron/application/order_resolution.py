"""Operator resolution of orders the reconciler cannot resolve (docs/failure-matrix.md).

Deliberately narrow:

- only ``SUBMISSION_UNKNOWN`` or ``RECONCILIATION_REQUIRED`` orders;
- only zero-fill outcomes (``NOT_PLACED``, ``CANCELED``, ``EXPIRED``,
  ``EXCHANGE_REJECTED``). Fills and positions only ever come from venue data,
  never from an operator's assertion;
- refused if the venue has any record of the order: then the venue's state
  must be applied (by reconciliation), not overridden;
- refused if the order already has recorded fills;
- recorded with the operator's name and note as an order event and a
  critical system event.
"""

import uuid
from collections.abc import Callable

from arbitron.domain.enums import OrderStatus, Severity
from arbitron.domain.events import OrderEvent, SystemEvent
from arbitron.domain.orders import Order
from arbitron.domain.risk import SYSTEM_ACTOR
from arbitron.ports.clock import Clock
from arbitron.ports.execution import ExecutionPort
from arbitron.ports.repositories import UnitOfWork

S = OrderStatus
RESOLVABLE = frozenset({S.SUBMISSION_UNKNOWN, S.RECONCILIATION_REQUIRED})
OPERATOR_OUTCOMES = frozenset({S.NOT_PLACED, S.CANCELED, S.EXPIRED, S.EXCHANGE_REJECTED})


class ResolutionRefusedError(Exception):
    pass


async def resolve_order(
    *,
    order: Order,
    outcome: OrderStatus,
    operator: str,
    note: str,
    execution: ExecutionPort,
    uow_factory: Callable[[], UnitOfWork],
    clock: Clock,
) -> Order:
    if not operator.strip() or operator.strip() == SYSTEM_ACTOR:
        msg = "a named operator is required"
        raise ResolutionRefusedError(msg)
    if not note.strip():
        msg = "a note explaining the resolution is required"
        raise ResolutionRefusedError(msg)
    if order.status not in RESOLVABLE:
        msg = f"order is {order.status}; only unknown or reconciliation-required orders"
        raise ResolutionRefusedError(msg)
    if outcome not in OPERATOR_OUTCOMES:
        allowed = ", ".join(sorted(o.value for o in OPERATOR_OUTCOMES))
        msg = f"operators may only resolve to a zero-fill outcome ({allowed})"
        raise ResolutionRefusedError(msg)
    venue_order = await execution.fetch_order_by_client_id(
        order.intent.client_order_id, order.intent.symbol
    )
    if venue_order is not None:
        msg = (
            f"the venue has this order ({venue_order.exchange_order_id}, "
            f"{venue_order.status}); let reconciliation apply the venue's state"
        )
        raise ResolutionRefusedError(msg)

    correlation_id = f"operator-{uuid.uuid4()}"
    async with uow_factory() as tx:
        if await tx.fills.list_for_order(order.id):
            msg = "order has recorded fills; it cannot be resolved as unfilled"
            raise ResolutionRefusedError(msg)
        updated = order.transition(outcome, at=max(clock.now(), order.updated_at))
        payload = {"operator": operator, "note": note}
        await tx.orders.apply_transition(
            order,
            updated,
            OrderEvent(
                order.id, order.status, outcome, updated.updated_at, correlation_id, payload
            ),
        )
        await tx.system_events.append(
            SystemEvent(
                event_type="order.operator_resolved",
                severity=Severity.CRITICAL,
                component="operator",
                occurred_at=updated.updated_at,
                correlation_id=correlation_id,
                payload={"order_id": str(order.id), "outcome": outcome.value, **payload},
            )
        )
        await tx.commit()
    return updated
