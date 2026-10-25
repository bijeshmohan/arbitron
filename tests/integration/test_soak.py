"""Soak evaluation over recorded system state (docs/soak-test.md)."""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncEngine

from arbitron.application.kill_switch import KillSwitchService
from arbitron.application.risk_gate import apply_risk_decision
from arbitron.application.soak import SoakEvaluator, SoakReport
from arbitron.clocks import ManualClock
from arbitron.domain.enums import OrderStatus, Severity, TimeInForce
from arbitron.domain.events import OrderEvent, SystemEvent
from arbitron.domain.fills import Fill
from arbitron.domain.orders import Order
from arbitron.domain.portfolio import EquitySnapshot
from arbitron.domain.risk import RiskDecision
from arbitron.persistence.db import create_session_factory
from tests.factories import (
    FIVE_MIN,
    SMA_CONFIG,
    T0,
    make_candle,
    make_intent,
    make_signal,
    make_trade,
)
from tests.integration.conftest import UowFactory

S = OrderStatus
HOURS = 2
START = T0
END = T0 + timedelta(hours=HOURS)
STEPS = HOURS * 12


async def healthy_soak(uow: UowFactory, *, skip_cycles: frozenset[int] = frozenset()) -> None:
    clock = ManualClock(START - timedelta(minutes=10))
    kill_switch = KillSwitchService(uow, clock)
    await kill_switch.ensure_initialized()
    await kill_switch.arm(operator="op", acknowledgement="soak start")
    async with uow() as tx:
        await tx.candles.upsert(
            [
                make_candle(open_time=START + FIVE_MIN.duration * i, ingested_at=END)
                for i in range(STEPS)
            ]
        )
        for i in range(STEPS):
            if i not in skip_cycles:
                await tx.equity_snapshots.add(
                    EquitySnapshot(
                        START + FIVE_MIN.duration * i + timedelta(seconds=6), "USDT", Decimal(1000)
                    )
                )
        await tx.heartbeats.upsert(
            component="trading", instance="default", beat_at=END, status="ok", detail=""
        )
        await tx.commit()


async def add_order(uow: UowFactory, status: OrderStatus, *, fill: str | None) -> None:
    signal = make_signal(source_candle_time=START, created_at=START + timedelta(minutes=5))
    intent = make_intent(
        signal_id=signal.id, created_at=signal.created_at, time_in_force=TimeInForce.IOC
    )
    at = intent.created_at
    async with uow() as tx:
        await tx.strategy_configs.add_if_absent(SMA_CONFIG, created_at=START)
        await tx.signals.add_if_absent(signal)
        order = Order.new(intent)
        await tx.orders.add_if_absent(order, OrderEvent(order.id, None, S.CREATED, at, "c"))
        approved = await apply_risk_decision(
            tx, order, RiskDecision(order.id, (), "t", at), correlation_id="c"
        )
        submitting = approved.transition(S.SUBMITTING, at=at)
        fields: dict[str, object] = {}
        if status is not S.SUBMISSION_UNKNOWN:
            fields = {"exchange_order_id": "ex-1"}
        if status is S.FILLED:
            fields |= {"filled_amount": intent.amount, "average_fill_price": Decimal(50000)}
        final = submitting.transition(status, at=at, **fields)  # type: ignore[arg-type]
        for before, after in ((approved, submitting), (submitting, final)):
            await tx.orders.apply_transition(
                before, after, OrderEvent(order.id, before.status, after.status, at, "c")
            )
        if fill is not None:
            trade = make_trade(exchange_order_id="ex-1", amount=Decimal(fill), timestamp=at)
            await tx.fills.add_if_absent(Fill(order.id, trade))
        await tx.commit()


async def evaluate(engine: AsyncEngine, *, end_now: bool = True) -> SoakReport:
    evaluator = SoakEvaluator(
        create_session_factory(engine),
        exchange_id="binance",
        symbols=("BTC/USDT",),
        timeframe=FIVE_MIN,
        quote_currency="USDT",
    )
    return await evaluator.evaluate(START, END, check_heartbeats=end_now)


def failures(report: SoakReport) -> list[str]:
    return [c.name for c in report.criteria if not c.passed]


async def test_healthy_soak_passes(engine: AsyncEngine, uow: UowFactory) -> None:
    await healthy_soak(uow)
    await add_order(uow, S.FILLED, fill="0.010")

    report = await evaluate(engine)

    assert failures(report) == []
    assert report.passed


async def test_missed_cycles_fail(engine: AsyncEngine, uow: UowFactory) -> None:
    await healthy_soak(uow, skip_cycles=frozenset({3, 4, 5}))

    report = await evaluate(engine)

    assert failures(report) == ["trading cycles"]
    cycles = next(c for c in report.criteria if c.name == "trading cycles")
    assert "21/24" in cycles.detail


async def test_unsettled_order_and_fill_mismatch_fail(engine: AsyncEngine, uow: UowFactory) -> None:
    await healthy_soak(uow)
    await add_order(uow, S.SUBMISSION_UNKNOWN, fill=None)

    assert failures(await evaluate(engine)) == ["orders settled"]


async def test_fills_not_matching_the_order_fail(engine: AsyncEngine, uow: UowFactory) -> None:
    await healthy_soak(uow)
    await add_order(uow, S.FILLED, fill="0.004")

    assert failures(await evaluate(engine)) == ["fills match orders"]


async def test_discrepancies_and_reviews_fail(engine: AsyncEngine, uow: UowFactory) -> None:
    await healthy_soak(uow)
    at = START + timedelta(minutes=30)
    async with uow() as tx:
        await tx.system_events.append(
            SystemEvent(
                "reconciliation.discrepancy", Severity.CRITICAL, "execution.reconciler", at, "c"
            )
        )
        await tx.commit()
    await KillSwitchService(uow, ManualClock(at)).begin_review(reason="reconciliation: 1")

    report = await evaluate(engine)

    assert failures(report) == [
        "reconciliation discrepancies",
        "no automatic kill-switch faults",
        "kill switch armed at end",
    ]
    assert report.critical_events == {"reconciliation.discrepancy": 1}


async def test_loss_limit_trip_is_not_a_fault(engine: AsyncEngine, uow: UowFactory) -> None:
    await healthy_soak(uow)
    clock = ManualClock(START + timedelta(minutes=30))
    kill_switch = KillSwitchService(uow, clock)
    await kill_switch.trip(reason="automatic: max_daily_loss (equity 989)")
    clock.advance(timedelta(minutes=10))
    await kill_switch.begin_review(reason="operator reviewing", actor="op")
    await kill_switch.arm(operator="op", acknowledgement="loss limit worked as designed")

    report = await evaluate(engine)

    assert failures(report) == []
    faults = next(c for c in report.criteria if c.name == "no automatic kill-switch faults")
    assert "1 loss-limit trip" in faults.detail


async def test_stale_heartbeat_fails_only_when_the_window_ends_now(
    engine: AsyncEngine, uow: UowFactory
) -> None:
    await healthy_soak(uow)
    async with uow() as tx:
        await tx.heartbeats.upsert(
            component="reconciliation",
            instance="default",
            beat_at=END - timedelta(hours=1),
            status="ok",
            detail="",
        )
        await tx.commit()

    assert failures(await evaluate(engine)) == ["tasks alive at end"]
    assert failures(await evaluate(engine, end_now=False)) == []


async def test_window_shorter_than_an_interval_fails_clearly(
    engine: AsyncEngine, uow: UowFactory
) -> None:
    await healthy_soak(uow)
    evaluator = SoakEvaluator(
        create_session_factory(engine),
        exchange_id="binance",
        symbols=("BTC/USDT",),
        timeframe=FIVE_MIN,
        quote_currency="USDT",
    )

    report = await evaluator.evaluate(START, START + timedelta(seconds=37), check_heartbeats=False)

    details = {c.name: c.detail for c in report.criteria if not c.passed}
    assert details == {
        "trading cycles": "window shorter than one 0:05:00 interval",
        "closed candles": "window too short to contain a closed candle",
    }
