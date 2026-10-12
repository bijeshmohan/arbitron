"""Signal -> order intent -> risk -> submission -> fills (plan §12.1).

Each step is persisted before the next side effect:

1. If an intent for this signal already exists, do nothing (idempotent; a
   restart never re-sizes or re-submits a signal).
2. Size the signal. "No intent" is recorded and ends here.
3. Persist the order as ``CREATED`` and its risk decision, moving it to
   ``RISK_APPROVED`` / ``RISK_REJECTED``, in one transaction.
4. Persist ``SUBMITTING``, then call the venue.
5. Record the venue's answer, the resulting fills, and the position update
   in one transaction.

Submission is never retried. A definitive venue rejection (invalid order,
insufficient funds, unavailable market, authentication) ends in
``EXCHANGE_REJECTED``. Anything ambiguous (timeout, network, rate limit,
unknown error) ends in ``SUBMISSION_UNKNOWN``: the order may or may not exist
on the venue, new orders are blocked (the snapshot reports reconciliation
as unresolved), and reconciliation (Phase 7) establishes the truth.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum

from arbitron.application.risk_gate import apply_risk_decision
from arbitron.domain.enums import OrderStatus, Severity
from arbitron.domain.events import OrderEvent, PayloadValue, SystemEvent
from arbitron.domain.fills import ExchangeTrade, Fill
from arbitron.domain.market import Market
from arbitron.domain.money import canonical_str
from arbitron.domain.orders import ExchangeOrder, Order, can_transition, intent_id_for
from arbitron.domain.signals import Signal
from arbitron.domain.snapshot import TradingSnapshot
from arbitron.portfolio.accounting import AccountingError, apply_fill
from arbitron.ports.clock import Clock
from arbitron.ports.errors import (
    AuthenticationFailure,
    InsufficientFunds,
    InvalidOrder,
    MarketUnavailable,
    UnsupportedCapability,
    VenueError,
)
from arbitron.ports.execution import ExecutionPort
from arbitron.ports.repositories import UnitOfWork
from arbitron.risk.manager import RiskPolicy, decide
from arbitron.sizing.sizer import SizingPolicy, size

COMPONENT = "execution.order_manager"
S = OrderStatus

# The venue definitively refused: the order does not exist there.
DEFINITIVE_REJECTIONS: tuple[type[VenueError], ...] = (
    InvalidOrder,
    InsufficientFunds,
    MarketUnavailable,
    AuthenticationFailure,
    UnsupportedCapability,
)
# How far before submission to look for the order's trades.
TRADE_LOOKBACK = timedelta(minutes=5)


class Action(StrEnum):
    ALREADY_HANDLED = "already_handled"
    NOT_SIZED = "not_sized"
    RISK_REJECTED = "risk_rejected"
    EXECUTED = "executed"  # venue answered; see the order status
    EXCHANGE_REJECTED = "exchange_rejected"
    SUBMISSION_UNKNOWN = "submission_unknown"


@dataclass(frozen=True, slots=True)
class OrderOutcome:
    signal_id: uuid.UUID
    action: Action
    order: Order | None = None
    detail: str = ""


class OrderManager:
    def __init__(
        self,
        *,
        execution: ExecutionPort,
        uow_factory: Callable[[], UnitOfWork],
        clock: Clock,
        sizing_policy: SizingPolicy,
        risk_policy: RiskPolicy,
        submission_timeout: timedelta,
        on_authentication_failure: Callable[[str], Awaitable[object]] | None = None,
    ) -> None:
        self._execution = execution
        # docs/failure-matrix.md #4: bad credentials will not fix themselves.
        self._on_auth_failure = on_authentication_failure
        self._uow_factory = uow_factory
        self._clock = clock
        self._sizing = sizing_policy
        self._risk = risk_policy
        self._timeout = submission_timeout.total_seconds()

    async def act_on_signal(self, signal: Signal, snapshot: TradingSnapshot) -> OrderOutcome:
        correlation_id = f"order-{uuid.uuid4()}"
        async with self._uow_factory() as tx:
            existing = await tx.orders.get(intent_id_for(signal.id))
        if existing is not None:
            return OrderOutcome(signal.id, Action.ALREADY_HANDLED, existing)

        now = self._clock.now()
        sized = size(signal, snapshot, self._sizing, now=now)
        if sized.intent is None:
            await self._record(
                "not_sized",
                Severity.INFO,
                correlation_id,
                signal_id=str(signal.id),
                reason=sized.reason.value,
                detail=sized.detail,
            )
            return OrderOutcome(signal.id, Action.NOT_SIZED, detail=sized.reason.value)

        order = Order.new(sized.intent)
        decision = decide(sized.intent, snapshot, self._risk, now=now)
        async with self._uow_factory() as tx:
            created = OrderEvent(order.id, None, S.CREATED, order.updated_at, correlation_id)
            if not await tx.orders.add_if_absent(order, created):
                return OrderOutcome(signal.id, Action.ALREADY_HANDLED, order)
            decided = await apply_risk_decision(tx, order, decision, correlation_id=correlation_id)
            if not decision.approved:
                await tx.system_events.append(
                    self._event(
                        "risk_rejected",
                        Severity.WARNING,
                        correlation_id,
                        order_id=str(order.id),
                        reasons=",".join(r.value for r in decision.reasons),
                    )
                )
            await tx.commit()
        if not decision.approved:
            return OrderOutcome(
                signal.id,
                Action.RISK_REJECTED,
                decided,
                ",".join(r.value for r in decision.reasons),
            )

        market = snapshot.markets[signal.symbol]
        return await self._submit(signal, decided, market, correlation_id)

    async def _submit(
        self, signal: Signal, approved: Order, market: Market, correlation_id: str
    ) -> OrderOutcome:
        submitting = await self._move(approved, S.SUBMITTING, correlation_id)
        try:
            async with asyncio.timeout(self._timeout):
                venue_order = await self._execution.submit_order(submitting.intent)
        except DEFINITIVE_REJECTIONS as exc:
            rejected = await self._move(
                submitting,
                S.EXCHANGE_REJECTED,
                correlation_id,
                severity=Severity.CRITICAL,
                error=f"{type(exc).__name__}: {exc}",
            )
            if isinstance(exc, AuthenticationFailure) and self._on_auth_failure is not None:
                await self._on_auth_failure(f"authentication failure on submission: {exc}")
            return OrderOutcome(signal.id, Action.EXCHANGE_REJECTED, rejected, str(exc))
        except (VenueError, TimeoutError) as exc:
            unknown = await self._move(
                submitting,
                S.SUBMISSION_UNKNOWN,
                correlation_id,
                severity=Severity.CRITICAL,
                error=f"{type(exc).__name__}: {exc}",
            )
            return OrderOutcome(signal.id, Action.SUBMISSION_UNKNOWN, unknown, str(exc))
        except BaseException:
            # A bug or cancellation mid-submission: the outcome is unknown. Record
            # that (shielded from cancellation) before propagating.
            await asyncio.shield(
                self._move(
                    submitting, S.SUBMISSION_UNKNOWN, correlation_id, severity=Severity.CRITICAL
                )
            )
            raise

        final = await self.record_venue_result(submitting, venue_order, market, correlation_id)
        return OrderOutcome(signal.id, Action.EXECUTED, final, final.status.value)

    async def record_venue_result(
        self, order: Order, venue_order: ExchangeOrder, market: Market, correlation_id: str
    ) -> Order:
        """Apply the venue's answer, its fills, and the position update atomically."""
        problems: list[str] = []
        if venue_order.client_order_id not in (None, order.intent.client_order_id):
            problems.append(f"venue returned client id {venue_order.client_order_id}")
        if not can_transition(order.status, venue_order.status):
            problems.append(
                f"venue status {venue_order.status} is not reachable from {order.status}"
            )

        trades: list[ExchangeTrade] = []
        try:
            # From submission time, not the last update: reconciliation may
            # resolve an order long after it was submitted.
            all_trades = await self._execution.fetch_trades(
                order.intent.symbol, since=order.intent.created_at - TRADE_LOOKBACK
            )
            trades = [t for t in all_trades if t.exchange_order_id == venue_order.exchange_order_id]
        except VenueError as exc:
            problems.append(f"fills not fetched: {exc}")
        traded = sum((t.amount for t in trades), Decimal(0))
        if not problems and traded != venue_order.filled:
            problems.append(f"trades sum to {traded} but venue reports {venue_order.filled} filled")
        if problems and order.status is S.RECONCILIATION_REQUIRED:
            # Already awaiting reconciliation: nothing new to record.
            return order

        now = max(self._clock.now(), order.updated_at)
        async with self._uow_factory() as tx:
            # Work out the position update first: an accounting failure must
            # route the order to reconciliation, not leave it terminal with a
            # position that does not match its fills.
            fills = [Fill(order.id, trade) for trade in trades]
            new_fills = [f for f in fills if await tx.fills.add_if_absent(f)]
            previous = await tx.positions.get(market.exchange_id, market.symbol)
            position = previous
            try:
                for fill in new_fills:
                    position = apply_fill(position, fill, market)
            except AccountingError as exc:
                problems.append(f"position not updated: {exc}")
                position = previous

            target = S.RECONCILIATION_REQUIRED if problems else venue_order.status
            fields: dict[str, object] = {"exchange_order_id": venue_order.exchange_order_id}
            if target is not S.RECONCILIATION_REQUIRED:
                fields |= {
                    "filled_amount": venue_order.filled,
                    "average_fill_price": venue_order.average_price,
                }
            updated = order.transition(target, at=now, **fields)  # type: ignore[arg-type]
            await tx.orders.apply_transition(
                order,
                updated,
                OrderEvent(
                    order.id,
                    order.status,
                    target,
                    now,
                    correlation_id,
                    {
                        "raw_status": venue_order.raw_status,
                        "filled": canonical_str(venue_order.filled),
                        "problems": "; ".join(problems) or None,
                    },
                ),
            )
            if position is not None and position is not previous:
                await tx.positions.save(previous, position)
            if problems:
                await tx.system_events.append(
                    self._event(
                        "venue_result_discrepancy",
                        Severity.CRITICAL,
                        correlation_id,
                        order_id=str(order.id),
                        detail="; ".join(problems),
                    )
                )
            await tx.commit()
        return updated

    async def _move(
        self,
        order: Order,
        target: OrderStatus,
        correlation_id: str,
        *,
        severity: Severity | None = None,
        **payload: PayloadValue,
    ) -> Order:
        updated = order.transition(target, at=max(self._clock.now(), order.updated_at))
        async with self._uow_factory() as tx:
            await tx.orders.apply_transition(
                order,
                updated,
                OrderEvent(
                    order.id, order.status, target, updated.updated_at, correlation_id, payload
                ),
            )
            if severity is not None:
                await tx.system_events.append(
                    self._event(
                        target.value, severity, correlation_id, order_id=str(order.id), **payload
                    )
                )
            await tx.commit()
        return updated

    def _event(
        self, kind: str, severity: Severity, correlation_id: str, **payload: PayloadValue
    ) -> SystemEvent:
        return SystemEvent(
            event_type=f"order.{kind}",
            severity=severity,
            component=COMPONENT,
            occurred_at=self._clock.now(),
            correlation_id=correlation_id,
            payload=payload,
        )

    async def _record(
        self, kind: str, severity: Severity, correlation_id: str, **payload: PayloadValue
    ) -> None:
        async with self._uow_factory() as tx:
            await tx.system_events.append(self._event(kind, severity, correlation_id, **payload))
            await tx.commit()
