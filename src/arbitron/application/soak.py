"""Paper soak evaluation (plan §22, Phase 12): pass/fail criteria over a window.

Read-only queries over what the running system already records. The
criteria and their thresholds are defined in ``docs/soak-test.md``; they are
operational (did the system run correctly and recover), never financial (no
criterion depends on profit or loss).

Not measurable from the database, and checked by the operator instead:
backup success, alert delivery, and the planned drills (docs/soak-test.md).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from arbitron.domain.enums import KillSwitchState, OrderStatus, Severity
from arbitron.domain.market import Timeframe
from arbitron.domain.orders import TERMINAL_STATUSES
from arbitron.domain.risk import SYSTEM_ACTOR
from arbitron.persistence.tables import (
    CandleRow,
    EquitySnapshotRow,
    FillRow,
    HeartbeatRow,
    KillSwitchEventRow,
    KillSwitchRow,
    OrderRow,
    SystemEventRow,
)

# Thresholds (docs/soak-test.md). Change them there and here together.
MIN_CYCLE_COVERAGE = Decimal("0.99")
MIN_CANDLE_COVERAGE = Decimal("0.99")
MAX_HEARTBEAT_AGE = timedelta(minutes=5)
# Orders younger than this may legitimately still be settling.
ORDER_SETTLE_GRACE = timedelta(minutes=15)
# Reason prefix of the session's automatic loss-limit trip (paper_session).
LOSS_LIMIT_PREFIX = "automatic: "


@dataclass(frozen=True, slots=True)
class Criterion:
    name: str
    passed: bool
    detail: str


@dataclass
class SoakReport:
    start: datetime
    end: datetime
    criteria: list[Criterion] = field(default_factory=list[Criterion])
    critical_events: dict[str, int] = field(default_factory=dict[str, int])

    @property
    def passed(self) -> bool:
        return bool(self.criteria) and all(c.passed for c in self.criteria)

    def add(self, name: str, passed: bool, detail: str) -> None:
        self.criteria.append(Criterion(name, passed, detail))


def _pct(value: Decimal) -> str:
    return f"{value * 100:.2f}%"


def _buckets(start: datetime, end: datetime, step: timedelta) -> int:
    return max(int((end - start) / step), 0)


def _candle_opens(start: datetime, end: datetime, step: timedelta) -> int:
    """Candle open times (aligned to the epoch) in ``[start, end)``."""
    epoch = datetime(1970, 1, 1, tzinfo=start.tzinfo)
    first = epoch + -((epoch - start) // step) * step  # ceil to the grid
    return max(-((first - end) // step), 0)


class SoakEvaluator:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        exchange_id: str,
        symbols: Sequence[str],
        timeframe: Timeframe,
        quote_currency: str,
    ) -> None:
        self._sessions = session_factory
        self._exchange_id = exchange_id
        self._symbols = tuple(symbols)
        self._timeframe = timeframe
        self._quote = quote_currency

    async def evaluate(
        self, start: datetime, end: datetime, *, check_heartbeats: bool = True
    ) -> SoakReport:
        """``check_heartbeats`` only makes sense when the window ends now."""
        report = SoakReport(start, end)
        async with self._sessions() as session:
            await self._cycles(session, report)
            await self._candles(session, report)
            await self._orders(session, report)
            await self._events(session, report)
            await self._kill_switch(session, report)
            if check_heartbeats:
                await self._heartbeats(session, report)
        return report

    async def _cycles(self, session: AsyncSession, report: SoakReport) -> None:
        """Trading cycles ran: an equity snapshot in (nearly) every candle interval."""
        step = self._timeframe.duration
        times = list(
            await session.scalars(
                select(EquitySnapshotRow.taken_at)
                .where(
                    EquitySnapshotRow.quote_currency == self._quote,
                    EquitySnapshotRow.taken_at >= report.start,
                    EquitySnapshotRow.taken_at < report.end,
                )
                .order_by(EquitySnapshotRow.taken_at)
            )
        )
        expected = _buckets(report.start, report.end, step)
        if not expected:
            report.add("trading cycles", False, f"window shorter than one {step} interval")
            return
        # Only complete intervals count; a cycle in the unfinished last one is ignored.
        covered = {i for t in times if (i := int((t - report.start) / step)) < expected}
        coverage = Decimal(len(covered)) / expected if expected else Decimal(0)
        edges = [report.start, *times, report.end]
        longest = max((b - a for a, b in pairwise(edges)), default=step)
        report.add(
            "trading cycles",
            coverage >= MIN_CYCLE_COVERAGE,
            f"{len(covered)}/{expected} intervals with a cycle ({_pct(coverage)}, "
            f"needs {_pct(MIN_CYCLE_COVERAGE)}); longest gap {longest}",
        )

    async def _candles(self, session: AsyncSession, report: SoakReport) -> None:
        step = self._timeframe.duration
        # Candles that opened in the window and had closed by its end.
        expected = _candle_opens(report.start, report.end - step + timedelta(microseconds=1), step)
        if not expected:
            report.add("closed candles", False, "window too short to contain a closed candle")
            return
        for symbol in self._symbols:
            stored = await session.scalar(
                select(func.count()).where(
                    CandleRow.exchange_id == self._exchange_id,
                    CandleRow.symbol == symbol,
                    CandleRow.timeframe == self._timeframe.value,
                    CandleRow.is_closed.is_(True),
                    CandleRow.open_time >= report.start,
                    CandleRow.open_time <= report.end - step,
                )
            )
            coverage = Decimal(stored or 0) / expected if expected else Decimal(0)
            report.add(
                f"closed candles {symbol}",
                coverage >= MIN_CANDLE_COVERAGE,
                f"{stored}/{expected} ({_pct(coverage)}, needs {_pct(MIN_CANDLE_COVERAGE)})",
            )

    async def _orders(self, session: AsyncSession, report: SoakReport) -> None:
        rows = list(
            await session.scalars(
                select(OrderRow).where(
                    OrderRow.exchange_id == self._exchange_id,
                    OrderRow.intent_created_at >= report.start,
                    OrderRow.intent_created_at < report.end,
                )
            )
        )
        terminal = {s.value for s in TERMINAL_STATUSES}
        settled_before = report.end - ORDER_SETTLE_GRACE
        unsettled = [
            r for r in rows if r.status not in terminal and r.intent_created_at < settled_before
        ]
        by_status: dict[str, int] = {}
        for row in rows:
            by_status[row.status] = by_status.get(row.status, 0) + 1
        report.add(
            "orders settled",
            not unsettled,
            f"{len(rows)} order(s): "
            + (", ".join(f"{k} {v}" for k, v in sorted(by_status.items())) or "none")
            + (f"; NOT SETTLED: {', '.join(str(r.id) for r in unsettled)}" if unsettled else ""),
        )

        sums = await session.execute(
            select(FillRow.order_id, func.sum(FillRow.amount))
            .where(FillRow.order_id.in_([r.id for r in rows]))
            .group_by(FillRow.order_id)
        )
        fills = {order_id: total for order_id, total in sums}
        mismatched = [
            f"{r.id} (filled {r.filled_amount}, fills {fills.get(r.id, 0)})"
            for r in rows
            if r.status in terminal and fills.get(r.id, Decimal(0)) != r.filled_amount
        ]
        report.add(
            "fills match orders",
            not mismatched,
            "every terminal order's fills sum to its filled amount"
            if not mismatched
            else "; ".join(mismatched),
        )
        rejected = by_status.get(OrderStatus.EXCHANGE_REJECTED.value, 0)
        report.add("no venue rejections", rejected == 0, f"{rejected} exchange_rejected order(s)")

    async def _events(self, session: AsyncSession, report: SoakReport) -> None:
        rows = await session.execute(
            select(SystemEventRow.event_type, func.count())
            .where(
                SystemEventRow.severity == Severity.CRITICAL.value,
                SystemEventRow.occurred_at >= report.start,
                SystemEventRow.occurred_at < report.end,
            )
            .group_by(SystemEventRow.event_type)
        )
        report.critical_events = {kind: count for kind, count in rows}
        events = report.critical_events

        def count(prefix: str) -> int:
            return sum(n for kind, n in events.items() if kind.startswith(prefix))

        discrepancies = count("reconciliation.discrepancy")
        report.add(
            "reconciliation discrepancies",
            discrepancies == 0,
            f"{discrepancies} discrepancy event(s)",
        )
        unknown = count("order.submission_unknown") + count("reconciliation.submission_unknown")
        report.add("ambiguous submissions", unknown == 0, f"{unknown} submission_unknown event(s)")
        crashes = count("supervisor.")
        report.add("task crashes", crashes == 0, f"{crashes} supervisor event(s)")

    async def _kill_switch(self, session: AsyncSession, report: SoakReport) -> None:
        automatic = list(
            await session.scalars(
                select(KillSwitchEventRow)
                .where(
                    KillSwitchEventRow.changed_at >= report.start,
                    KillSwitchEventRow.changed_at < report.end,
                    KillSwitchEventRow.actor == SYSTEM_ACTOR,
                    # A fresh database starts in review; that is not a fault.
                    KillSwitchEventRow.from_state.is_not(None),
                    KillSwitchEventRow.to_state != KillSwitchState.ARMED.value,
                )
                .order_by(KillSwitchEventRow.changed_at)
            )
        )
        # A loss-limit trip is the risk engine working, not a fault; anything
        # else the system did on its own (reconciliation review, restart limit,
        # authentication failure) fails the soak.
        limits = [e for e in automatic if e.reason.startswith(LOSS_LIMIT_PREFIX)]
        faults = [e for e in automatic if e not in limits]
        report.add(
            "no automatic kill-switch faults",
            not faults,
            "; ".join(f"{e.changed_at:%Y-%m-%d %H:%M} {e.to_state}: {e.reason}" for e in faults)
            or f"none ({len(limits)} loss-limit trip(s), reported separately)",
        )
        current = await session.scalar(select(KillSwitchRow.state))
        report.add(
            "kill switch armed at end",
            current == KillSwitchState.ARMED.value,
            f"state {current}",
        )

    async def _heartbeats(self, session: AsyncSession, report: SoakReport) -> None:
        rows = list(await session.scalars(select(HeartbeatRow)))
        stale = [
            f"{r.component} ({report.end - r.last_beat})"
            for r in rows
            if report.end - r.last_beat > MAX_HEARTBEAT_AGE
        ]
        report.add(
            "tasks alive at end",
            bool(rows) and not stale,
            f"{len(rows)} task(s) reporting"
            + (f"; stale: {', '.join(stale)}" if stale else "")
            + ("" if rows else "; no heartbeats recorded"),
        )
