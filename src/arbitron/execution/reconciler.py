"""Reconciliation: compare the bot's records with the venue's (plan §14).

Implements the operator-approved rules in ``docs/failure-matrix.md``:

1. ``recover_interrupted`` (startup only): ``SUBMITTING`` orders became
   ambiguous when the process stopped; they move to ``SUBMISSION_UNKNOWN``.
2. ``SUBMISSION_UNKNOWN``: look the order up by client order ID. Found ->
   apply the venue's state and fills. Not found -> ``NOT_PLACED`` only after
   the settle window *and* two consecutive not-found lookups. Unresolved past
   the max age -> ``RECONCILIATION_REQUIRED`` and a critical event.
3. ``RECONCILIATION_REQUIRED``: re-fetch from the venue; apply automatically
   only if the venue state, its trades, and local fills now agree.
4. Fill sync: venue trades (overlapping window) missing locally are inserted
   if they belong to a known order and stay within its venue-reported fill
   (deterministic repair); anything else is a discrepancy.
5. Holdings: the venue's base balance must equal the projected position for
   each symbol without unresolved orders.
6. Unknown venue orders: an open venue order with no local record.

Discrepancies are recorded as critical events and move an ``ARMED`` kill
switch to ``RECOVERY_REVIEW``; a named operator must re-arm. The reconciler
never creates trades and never rewrites recorded fills or positions to match
the venue (plan §14.3).
"""

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from arbitron.application.kill_switch import KillSwitchService
from arbitron.domain.enums import KillSwitchState, OrderStatus, Severity
from arbitron.domain.events import OrderEvent, PayloadValue, SystemEvent
from arbitron.domain.fills import ExchangeTrade, Fill
from arbitron.domain.market import Market
from arbitron.domain.money import canonical_str
from arbitron.domain.orders import ExchangeOrder, Order
from arbitron.execution.order_manager import OrderManager
from arbitron.portfolio.accounting import AccountingError, apply_fill
from arbitron.ports.account import AccountPort
from arbitron.ports.clock import Clock
from arbitron.ports.errors import UnsupportedCapability, VenueError
from arbitron.ports.execution import ExecutionPort
from arbitron.ports.repositories import UnitOfWork

COMPONENT = "execution.reconciler"
S = OrderStatus
REQUIRED_NOT_FOUND_CHECKS = 2
# Trade fetches overlap the previous window so nothing falls between runs.
TRADE_OVERLAP = timedelta(minutes=10)
INITIAL_TRADE_WINDOW = timedelta(hours=24)


@dataclass
class ReconciliationReport:
    started_at: datetime
    resolved: list[str] = field(default_factory=list[str])  # order states established
    repairs: list[str] = field(default_factory=list[str])  # deterministic repairs applied
    pending: list[str] = field(default_factory=list[str])  # waiting (settle window, venue)
    discrepancies: list[str] = field(default_factory=list[str])  # operator needed

    @property
    def clean(self) -> bool:
        return not self.pending and not self.discrepancies


class Reconciler:
    def __init__(
        self,
        *,
        execution: ExecutionPort,
        account: AccountPort,
        order_manager: OrderManager,
        kill_switch: KillSwitchService,
        uow_factory: Callable[[], UnitOfWork],
        clock: Clock,
        markets: Callable[[], Mapping[str, Market]],
        exchange_id: str,
        symbols: tuple[str, ...],
        settle: timedelta,
        max_age: timedelta,
    ) -> None:
        self._execution = execution
        self._account = account
        self._orders = order_manager
        self._kill_switch = kill_switch
        self._uow_factory = uow_factory
        self._clock = clock
        self._markets = markets
        self._exchange_id = exchange_id
        self._symbols = symbols
        self._settle = settle
        self._max_age = max_age
        self._last_trade_sync: datetime | None = None
        self._reported: set[str] = set()  # discrepancies already alerted on

    async def recover_interrupted(self) -> list[Order]:
        """Startup: orders left in SUBMITTING may or may not exist on the venue."""
        recovered: list[Order] = []
        async with self._uow_factory() as tx:
            stuck = [
                o
                for o in await tx.orders.list_non_terminal(self._exchange_id)
                if o.status is S.SUBMITTING
            ]
        for order in stuck:
            recovered.append(
                await self._move(
                    order,
                    S.SUBMISSION_UNKNOWN,
                    Severity.CRITICAL,
                    reason="process stopped during submission",
                )
            )
        return recovered

    async def run(self) -> ReconciliationReport:
        report = ReconciliationReport(started_at=self._clock.now())
        async with self._uow_factory() as tx:
            open_orders = list(await tx.orders.list_non_terminal(self._exchange_id))
        for order in open_orders:
            if order.status is S.SUBMISSION_UNKNOWN:
                await self._resolve_unknown(order, report)
            elif order.status is S.RECONCILIATION_REQUIRED:
                await self._recheck(order, report)
        await self._sync_fills(report)
        await self._check_holdings(report)
        await self._check_unknown_venue_orders(report)
        await self._escalate(report)
        return report

    # --- step 2: unknown submissions ----------------------------------------

    async def _resolve_unknown(self, order: Order, report: ReconciliationReport) -> None:
        now = self._clock.now()
        age = now - order.updated_at
        try:
            venue_order = await self._execution.fetch_order_by_client_id(
                order.intent.client_order_id, order.intent.symbol
            )
        except UnsupportedCapability:
            report.pending.append(f"{order.id}: venue cannot look orders up by client id")
            await self._maybe_escalate_age(order, age, report)
            return
        except VenueError as exc:
            report.pending.append(f"{order.id}: venue lookup failed ({exc})")
            await self._maybe_escalate_age(order, age, report)
            return

        async with self._uow_factory() as tx:
            await tx.reconciliation_lookups.record(
                order.id, checked_at=now, found=venue_order is not None
            )
            misses = await tx.reconciliation_lookups.not_found_since(
                order.id, order.updated_at + self._settle
            )
            await tx.commit()

        if venue_order is not None:
            final = await self._apply_venue_state(order, venue_order, report)
            if final is not None:
                report.resolved.append(f"{order.id}: {S.SUBMISSION_UNKNOWN} -> {final.status}")
            return
        if age >= self._settle and misses >= REQUIRED_NOT_FOUND_CHECKS:
            await self._move(
                order,
                S.NOT_PLACED,
                Severity.INFO,
                reason=f"not on the venue after {age} and {misses} lookups",
            )
            report.resolved.append(f"{order.id}: {S.SUBMISSION_UNKNOWN} -> {S.NOT_PLACED}")
            return
        report.pending.append(
            f"{order.id}: not found yet ({misses}/{REQUIRED_NOT_FOUND_CHECKS} checks "
            f"after the {self._settle} settle window)"
        )
        await self._maybe_escalate_age(order, age, report)

    async def _maybe_escalate_age(
        self, order: Order, age: timedelta, report: ReconciliationReport
    ) -> None:
        if age < self._max_age:
            return
        await self._move(
            order,
            S.RECONCILIATION_REQUIRED,
            Severity.CRITICAL,
            reason=f"submission outcome unknown for {age}; operator must resolve",
        )
        report.discrepancies.append(f"{order.id}: unresolved for {age}, escalated to operator")

    # --- step 3: orders needing reconciliation -------------------------------

    async def _recheck(self, order: Order, report: ReconciliationReport) -> None:
        try:
            if order.exchange_order_id is not None:
                venue_order = await self._execution.fetch_order(
                    order.exchange_order_id, order.intent.symbol
                )
            else:
                venue_order = await self._execution.fetch_order_by_client_id(
                    order.intent.client_order_id, order.intent.symbol
                )
        except VenueError as exc:
            report.pending.append(f"{order.id}: venue lookup failed ({exc})")
            return
        if venue_order is None:
            report.discrepancies.append(f"{order.id}: needs reconciliation; venue has no record")
            return
        problem = await self._venue_problem(order, venue_order)
        if problem is not None:
            report.discrepancies.append(f"{order.id}: needs reconciliation; {problem}")
            return
        final = await self._apply_venue_state(order, venue_order, report)
        if final is not None:
            report.resolved.append(f"{order.id}: {S.RECONCILIATION_REQUIRED} -> {final.status}")

    async def _venue_problem(self, order: Order, venue_order: ExchangeOrder) -> str | None:
        if venue_order.client_order_id not in (None, order.intent.client_order_id):
            return f"venue client id {venue_order.client_order_id} does not match"
        try:
            trades = await self._order_trades(order, venue_order.exchange_order_id)
        except VenueError as exc:
            return f"trades unavailable ({exc})"
        traded = sum((t.amount for t in trades), Decimal(0))
        if traded != venue_order.filled:
            return f"venue trades sum to {traded} but it reports {venue_order.filled} filled"
        return None

    async def _apply_venue_state(
        self, order: Order, venue_order: ExchangeOrder, report: ReconciliationReport
    ) -> Order | None:
        market = self._markets().get(order.intent.symbol)
        if market is None:
            report.pending.append(f"{order.id}: market metadata unavailable")
            return None
        if order.status is S.RECONCILIATION_REQUIRED and await self._venue_problem(
            order, venue_order
        ):
            return None  # caller already reported it
        final = await self._orders.record_venue_result(
            order, venue_order, market, f"reconcile-{uuid.uuid4()}"
        )
        if final.status is S.RECONCILIATION_REQUIRED:
            report.discrepancies.append(f"{order.id}: venue answer inconsistent; needs operator")
            return None
        return final

    # --- step 4: fill sync ----------------------------------------------------

    async def _sync_fills(self, report: ReconciliationReport) -> None:
        now = self._clock.now()
        since = (self._last_trade_sync or now - INITIAL_TRADE_WINDOW) - TRADE_OVERLAP
        markets = self._markets()
        complete = True
        for symbol in self._symbols:
            market = markets.get(symbol)
            if market is None:
                report.pending.append(f"{symbol}: market metadata unavailable for fill sync")
                complete = False
                continue
            try:
                trades = await self._execution.fetch_trades(symbol, since=since)
            except VenueError as exc:
                report.pending.append(f"{symbol}: trades unavailable ({exc})")
                complete = False
                continue
            for trade in trades:
                await self._sync_trade(trade, market, report)
        if complete:
            self._last_trade_sync = now

    async def _sync_trade(
        self, trade: ExchangeTrade, market: Market, report: ReconciliationReport
    ) -> None:
        async with self._uow_factory() as tx:
            order = await tx.orders.get_by_exchange_order_id(
                self._exchange_id, trade.exchange_order_id
            )
            if order is None:
                report.discrepancies.append(
                    f"venue trade {trade.dedupe_key} belongs to unknown order "
                    f"{trade.exchange_order_id}"
                )
                return
            if order.status in (S.SUBMISSION_UNKNOWN, S.RECONCILIATION_REQUIRED):
                return  # resolved through the order, not trade by trade
            existing = await tx.fills.list_for_order(order.id)
            if any(f.dedupe_key == trade.dedupe_key for f in existing):
                return
            recorded = sum((f.trade.amount for f in existing), Decimal(0))
            if recorded + trade.amount > order.filled_amount:
                report.discrepancies.append(
                    f"{order.id}: venue trade {trade.dedupe_key} would exceed the "
                    f"recorded fill of {order.filled_amount}"
                )
                return
            fill = Fill(order.id, trade)
            previous = await tx.positions.get(market.exchange_id, market.symbol)
            try:
                updated = apply_fill(previous, fill, market)
            except AccountingError as exc:
                report.discrepancies.append(f"{order.id}: cannot apply missing fill: {exc}")
                return
            await tx.fills.add_if_absent(fill)
            await tx.positions.save(previous, updated)
            await tx.system_events.append(
                self._event(
                    "fill_repaired",
                    Severity.WARNING,
                    order_id=str(order.id),
                    trade=trade.dedupe_key,
                    amount=canonical_str(trade.amount),
                )
            )
            await tx.commit()
        report.repairs.append(f"{order.id}: inserted missing fill {trade.dedupe_key}")

    # --- steps 5 and 6: account-level checks ---------------------------------

    async def _check_holdings(self, report: ReconciliationReport) -> None:
        try:
            balances = await self._account.fetch_balances()
        except VenueError as exc:
            report.pending.append(f"balances unavailable ({exc})")
            return
        markets = self._markets()
        async with self._uow_factory() as tx:
            unresolved = {
                o.intent.symbol
                for o in await tx.orders.list_non_terminal(self._exchange_id)
                if o.status in (S.SUBMITTING, S.SUBMISSION_UNKNOWN, S.RECONCILIATION_REQUIRED)
            }
            for symbol in self._symbols:
                market = markets.get(symbol)
                if market is None or symbol in unresolved:
                    continue  # holdings legitimately in flight
                position = await tx.positions.get(self._exchange_id, symbol)
                local = position.quantity if position is not None else Decimal(0)
                balance = balances.get(market.base)
                venue = balance.total if balance is not None else Decimal(0)
                if venue != local:
                    report.discrepancies.append(
                        f"{symbol}: venue holds {canonical_str(venue)} {market.base}, "
                        f"bot's position is {canonical_str(local)}"
                    )

    async def _check_unknown_venue_orders(self, report: ReconciliationReport) -> None:
        try:
            venue_open = await self._execution.fetch_open_orders()
        except VenueError as exc:
            report.pending.append(f"open orders unavailable ({exc})")
            return
        async with self._uow_factory() as tx:
            for venue_order in venue_open:
                known = None
                if venue_order.client_order_id:
                    known = await tx.orders.get_by_client_order_id(
                        self._exchange_id, venue_order.client_order_id
                    )
                if known is None:
                    report.discrepancies.append(
                        f"unknown open order on the venue: {venue_order.exchange_order_id} "
                        f"({venue_order.symbol}, client id {venue_order.client_order_id})"
                    )

    # --- discrepancy handling -------------------------------------------------

    async def _escalate(self, report: ReconciliationReport) -> None:
        new = [d for d in report.discrepancies if d not in self._reported]
        self._reported = set(report.discrepancies)
        if not report.discrepancies:
            return
        if new:
            async with self._uow_factory() as tx:
                for detail in new:
                    await tx.system_events.append(
                        self._event("discrepancy", Severity.CRITICAL, detail=detail)
                    )
                await tx.commit()
        state = await self._kill_switch.status()
        if state is not None and state.state is KillSwitchState.ARMED:
            await self._kill_switch.begin_review(
                reason=f"reconciliation: {len(report.discrepancies)} discrepancy(ies): "
                + "; ".join(report.discrepancies)[:500]
            )

    # --- helpers --------------------------------------------------------------

    async def _order_trades(self, order: Order, exchange_order_id: str) -> list[ExchangeTrade]:
        trades = await self._execution.fetch_trades(
            order.intent.symbol, since=order.intent.created_at - TRADE_OVERLAP
        )
        return [t for t in trades if t.exchange_order_id == exchange_order_id]

    async def _move(
        self, order: Order, target: OrderStatus, severity: Severity, *, reason: str
    ) -> Order:
        correlation_id = f"reconcile-{uuid.uuid4()}"
        updated = order.transition(target, at=max(self._clock.now(), order.updated_at))
        async with self._uow_factory() as tx:
            await tx.orders.apply_transition(
                order,
                updated,
                OrderEvent(
                    order.id,
                    order.status,
                    target,
                    updated.updated_at,
                    correlation_id,
                    {"reason": reason},
                ),
            )
            await tx.system_events.append(
                self._event(target.value, severity, order_id=str(order.id), reason=reason)
            )
            await tx.commit()
        return updated

    def _event(self, kind: str, severity: Severity, **payload: PayloadValue) -> SystemEvent:
        return SystemEvent(
            event_type=f"reconciliation.{kind}",
            severity=severity,
            component=COMPONENT,
            occurred_at=self._clock.now(),
            correlation_id=f"reconcile-{uuid.uuid4()}",
            payload=payload,
        )
