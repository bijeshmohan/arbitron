"""Failure injection, one test (or group) per row of docs/failure-matrix.md.

Rows covered elsewhere:
  #7  duplicates          tests/integration/test_paper_trading.py (restart, no duplicates)
  #11 stale market data   tests/integration/test_market_ingestion.py, test_strategy_runner.py
  #13 risk raises         tests/unit/test_risk_manager.py::TestRobustness
  #15 lock lost           tests/integration/test_lock.py; `paper run` checks it every cycle
  #17 loss limits         tests/integration/test_paper_trading.py (automatic trip)
"""

from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from arbitron.application.order_resolution import ResolutionRefusedError, resolve_order
from arbitron.application.paper_session import PaperSession
from arbitron.application.risk_gate import apply_risk_decision
from arbitron.clocks import ManualClock
from arbitron.config.settings import ArbitronConfig
from arbitron.domain.enums import KillSwitchState, OrderStatus, OrderType, Side, TimeInForce
from arbitron.domain.events import OrderEvent
from arbitron.domain.orders import ExchangeOrder, Order, OrderIntent
from arbitron.domain.risk import RiskDecision
from arbitron.execution.order_manager import Action
from arbitron.paper.broker import PaperBroker
from arbitron.persistence.db import create_session_factory
from arbitron.ports import errors as venue
from arbitron.ports.execution import ExecutionPort
from arbitron.simulation.fill_model import FillModel
from tests.factories import SMA_CONFIG, T0, make_intent, make_signal
from tests.integration.conftest import UowFactory
from tests.integration.test_market_ingestion import MARKET, NOW, FakeVenue, history
from tests.integration.test_paper_trading import MODEL, ScriptedStrategy, candle_time, config

S = OrderStatus
SYMBOL = "BTC/USDT"
SETTLE = timedelta(seconds=30)


class FaultyVenue:
    """Wraps the paper venue for submission and injects a failure.

    - ``before``: raise without the venue ever seeing the order;
    - ``after``:  the venue accepts (and fills) the order, then the response is lost.
    """

    def __init__(self, broker: PaperBroker, *, error: Exception, when: str) -> None:
        self._broker = broker
        self._error = error
        self._when = when

    async def submit_order(self, intent: OrderIntent) -> ExchangeOrder:
        if self._when == "after":
            await self._broker.submit_order(intent)
        raise self._error

    def __getattr__(self, name: str) -> Any:
        return getattr(self._broker, name)


async def session_for(
    engine: AsyncEngine,
    uow: UowFactory,
    clock: ManualClock,
    *,
    fault: tuple[Exception, str] | None = None,
    model: FillModel = MODEL,
    cfg: ArbitronConfig | None = None,
    arm: bool = True,
    script: dict[int, Side] | None = None,
) -> PaperSession:
    venue_ = FakeVenue(clock, history(200))

    def submission(broker: PaperBroker) -> ExecutionPort:
        assert fault is not None
        return FaultyVenue(broker, error=fault[0], when=fault[1])  # type: ignore[return-value]

    session = PaperSession(
        cfg or config(),
        strategy=ScriptedStrategy(script if script is not None else {99: Side.BUY}),  # type: ignore[arg-type]
        market_data=venue_,
        session_factory=create_session_factory(engine),
        uow_factory=uow,
        clock=clock,
        fill_model=model,
        submission_port=submission if fault else None,
    )
    await session.start()
    state = await session.kill_switch.status()
    if arm and state is not None and state.state is not KillSwitchState.ARMED:
        if state.state is KillSwitchState.TRIPPED:
            await session.kill_switch.begin_review(reason="test")
        await session.kill_switch.arm(operator="tester", acknowledgement="failure test")
    return session


async def only_order(uow: UowFactory) -> Order:
    async with uow() as tx:
        orders = await tx.orders.list_recent("binance", 10)
    assert len(orders) == 1, orders
    return orders[0]


async def scalar(engine: AsyncEngine, sql: str) -> Any:
    async with engine.connect() as conn:
        return await conn.scalar(text(sql))


async def kill_switch_state(session: PaperSession) -> KillSwitchState:
    state = await session.kill_switch.status()
    assert state is not None
    return state.state


async def assert_holdings_match(session: PaperSession, uow: UowFactory) -> None:
    balances = await session.broker.fetch_balances()
    async with uow() as tx:
        position = await tx.positions.get("binance", SYMBOL)
    venue_btc = balances["BTC"].total if "BTC" in balances else Decimal(0)
    assert venue_btc == (position.quantity if position else Decimal(0))


# --- #1 / #3: request never reached the venue ----------------------------------


@pytest.mark.parametrize(
    "error",
    [venue.RequestTimeout("timed out"), venue.RateLimited("429")],
    ids=["timeout", "rate-limit"],
)
async def test_1_3_unsent_order_becomes_not_placed_after_settle_and_two_checks(
    engine: AsyncEngine, uow: UowFactory, error: Exception
) -> None:
    clock = ManualClock(NOW)
    session = await session_for(engine, uow, clock, fault=(error, "before"))

    outcome = await session.run_cycle()

    assert [o.action for o in outcome.orders] == [Action.SUBMISSION_UNKNOWN]
    assert outcome.reconciliation is not None
    assert not outcome.reconciliation.clean  # within the settle window: still pending

    # A check after the settle window: one miss is not enough.
    clock.advance(SETTLE + timedelta(seconds=1))
    first = await session.reconciler.run()
    assert (await only_order(uow)).status is S.SUBMISSION_UNKNOWN
    assert not first.clean

    clock.advance(timedelta(seconds=10))
    second = await session.reconciler.run()
    assert (await only_order(uow)).status is S.NOT_PLACED
    assert second.clean
    assert await scalar(engine, "SELECT count(*) FROM reconciliation_lookups WHERE NOT found") == 3
    assert await scalar(engine, "SELECT count(*) FROM paper_orders") == 0


# --- #2: venue accepted, response lost ----------------------------------------


async def test_2_lost_response_is_recovered_from_the_venue(
    engine: AsyncEngine, uow: UowFactory
) -> None:
    session = await session_for(
        engine, uow, ManualClock(NOW), fault=(venue.RequestTimeout("lost"), "after")
    )

    outcome = await session.run_cycle()

    assert [o.action for o in outcome.orders] == [Action.SUBMISSION_UNKNOWN]
    assert outcome.reconciliation is not None
    assert outcome.reconciliation.clean  # reconciled straight away
    order = await only_order(uow)
    assert order.status is S.FILLED
    assert order.exchange_order_id is not None
    assert await scalar(engine, "SELECT count(*) FROM fills") == 1
    await assert_holdings_match(session, uow)


# --- #4: authentication failure -------------------------------------------------


async def test_4_authentication_failure_rejects_and_trips_kill_switch(
    engine: AsyncEngine, uow: UowFactory
) -> None:
    session = await session_for(
        engine, uow, ManualClock(NOW), fault=(venue.AuthenticationFailure("key revoked"), "before")
    )

    outcome = await session.run_cycle()

    assert [o.action for o in outcome.orders] == [Action.EXCHANGE_REJECTED]
    assert await kill_switch_state(session) is KillSwitchState.TRIPPED


# --- #5: database unavailable ----------------------------------------------------


async def test_5_database_outage_stops_the_cycle_before_any_order(
    engine: AsyncEngine, uow: UowFactory
) -> None:
    clock = ManualClock(NOW)
    down = False

    def flaky_uow() -> Any:
        if down:
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))
        return uow()

    session = await session_for(engine, flaky_uow, clock)  # type: ignore[arg-type]
    down = True

    with pytest.raises(OperationalError):
        await session.run_cycle()

    assert await scalar(engine, "SELECT count(*) FROM paper_orders") == 0
    assert await scalar(engine, "SELECT count(*) FROM orders") == 0


# --- #6: process killed during submission ------------------------------------------


async def stranded_submitting_order(
    engine: AsyncEngine, uow: UowFactory, *, venue_accepts: bool, model: FillModel = MODEL
) -> Order:
    """Leave an order in SUBMITTING, as a crash mid-submission would."""
    intent = make_intent(
        signal_id=make_signal(source_candle_time=candle_time(99)).id,
        amount=Decimal("0.249"),
        limit_price=Decimal("200.00"),
        time_in_force=TimeInForce.IOC,
        order_type=OrderType.LIMIT,
        created_at=NOW,
    )
    order = Order.new(intent)
    async with uow() as tx:
        await tx.strategy_configs.add_if_absent(SMA_CONFIG, created_at=T0)
        await tx.signals.add_if_absent(
            make_signal(source_candle_time=candle_time(99), created_at=NOW)
        )
        await tx.orders.add_if_absent(order, OrderEvent(order.id, None, S.CREATED, NOW, "c"))
        approved = await apply_risk_decision(
            tx, order, RiskDecision(order.id, (), "t", NOW), correlation_id="c"
        )
        submitting = approved.transition(S.SUBMITTING, at=NOW)
        await tx.orders.apply_transition(
            approved, submitting, OrderEvent(order.id, approved.status, S.SUBMITTING, NOW, "c")
        )
        await tx.commit()
    broker = PaperBroker(
        session_factory=create_session_factory(engine),
        account="default",
        exchange_id="binance",
        quote_currency="USDT",
        market_data=FakeVenue(ManualClock(NOW), history(200)),
        markets=lambda: {SYMBOL: MARKET},
        fill_model=model,
        clock=ManualClock(NOW),
    )
    await broker.ensure_funded(Decimal(1000))
    if venue_accepts:
        await broker.submit_order(intent)
    return submitting


async def test_6_restart_recovers_an_order_the_venue_accepted(
    engine: AsyncEngine, uow: UowFactory
) -> None:
    await stranded_submitting_order(engine, uow, venue_accepts=True)

    session = await session_for(engine, uow, ManualClock(NOW), script={})

    order = await only_order(uow)
    assert order.status is S.FILLED
    assert await scalar(engine, "SELECT count(*) FROM fills") == 1
    await assert_holdings_match(session, uow)
    assert (
        await scalar(
            engine,
            "SELECT count(*) FROM system_events "
            "WHERE event_type = 'reconciliation.submission_unknown'",
        )
        == 1
    )


async def test_6_restart_marks_never_sent_order_not_placed(
    engine: AsyncEngine, uow: UowFactory
) -> None:
    clock = ManualClock(NOW)
    await stranded_submitting_order(engine, uow, venue_accepts=False)
    session = await session_for(engine, uow, clock, script={})
    assert (await only_order(uow)).status is S.SUBMISSION_UNKNOWN

    for _ in range(2):
        clock.advance(SETTLE)
        await session.reconciler.run()

    assert (await only_order(uow)).status is S.NOT_PLACED


# --- #8: partial fills, including across a restart (Phase 7 acceptance) -------------


async def test_8_partial_fill_is_closed_and_matches_venue(
    engine: AsyncEngine, uow: UowFactory
) -> None:
    half = FillModel(slippage_bps=Decimal(5), fee_rate=Decimal("0.001"), fill_ratio=Decimal("0.5"))
    session = await session_for(engine, uow, ManualClock(NOW), model=half)

    outcome = await session.run_cycle()

    order = outcome.orders[0].order
    assert order is not None
    assert order.status is S.PARTIALLY_FILLED_CLOSED
    assert 0 < order.filled_amount < order.intent.amount
    await assert_holdings_match(session, uow)


async def test_8_restart_with_partially_filled_order_recovers_safely(
    engine: AsyncEngine, uow: UowFactory
) -> None:
    half = FillModel(slippage_bps=Decimal(5), fee_rate=Decimal("0.001"), fill_ratio=Decimal("0.5"))
    await stranded_submitting_order(engine, uow, venue_accepts=True, model=half)

    session = await session_for(engine, uow, ManualClock(NOW), script={})

    order = await only_order(uow)
    assert order.status is S.PARTIALLY_FILLED_CLOSED
    assert order.filled_amount == Decimal("0.12450")
    assert session.reconciler is not None
    await assert_holdings_match(session, uow)
    report = await session.reconciler.run()
    assert report.clean


# --- #9: a fill the bot never recorded -------------------------------------------


async def test_9_missing_fill_for_known_order_is_repaired(
    engine: AsyncEngine, uow: UowFactory
) -> None:
    submitting = await stranded_submitting_order(engine, uow, venue_accepts=True)
    broker_order = await PaperBroker(
        session_factory=create_session_factory(engine),
        account="default",
        exchange_id="binance",
        quote_currency="USDT",
        market_data=FakeVenue(ManualClock(NOW), history(200)),
        markets=lambda: {SYMBOL: MARKET},
        fill_model=MODEL,
        clock=ManualClock(NOW),
    ).fetch_order_by_client_id(submitting.intent.client_order_id, SYMBOL)
    assert broker_order is not None
    # The order's final state was recorded, but its fill write was lost.
    filled = submitting.transition(
        S.FILLED,
        at=NOW,
        exchange_order_id=broker_order.exchange_order_id,
        filled_amount=broker_order.filled,
        average_fill_price=broker_order.average_price,
    )
    async with uow() as tx:
        await tx.orders.apply_transition(
            submitting, filled, OrderEvent(filled.id, S.SUBMITTING, S.FILLED, NOW, "c")
        )
        await tx.commit()

    session = await session_for(engine, uow, ManualClock(NOW), script={})

    assert await scalar(engine, "SELECT count(*) FROM fills") == 1
    assert (
        await scalar(
            engine,
            "SELECT count(*) FROM system_events WHERE event_type = 'reconciliation.fill_repaired'",
        )
        == 1
    )
    await assert_holdings_match(session, uow)
    assert await kill_switch_state(session) is KillSwitchState.ARMED


# --- #10 / #14: holdings disagree -------------------------------------------------


async def test_10_holdings_mismatch_moves_kill_switch_to_review(
    engine: AsyncEngine, uow: UowFactory
) -> None:
    clock = ManualClock(NOW)
    session = await session_for(engine, uow, clock, script={})
    async with engine.begin() as conn:
        # Venue holds BTC the bot has no record of (e.g. trades outside its history window).
        await conn.execute(
            text(
                "INSERT INTO paper_balances (account, asset, free, used) "
                "VALUES ('default', 'BTC', 0.5, 0)"
            )
        )

    report = await session.reconciler.run()

    assert any("venue holds 0.5 BTC" in d for d in report.discrepancies)
    assert await kill_switch_state(session) is KillSwitchState.RECOVERY_REVIEW
    assert (
        await scalar(
            engine,
            "SELECT count(*) FROM system_events WHERE event_type = 'reconciliation.discrepancy'",
        )
        == 1
    )
    # Repeated runs do not repeat the alert while the discrepancy persists.
    await session.reconciler.run()
    assert (
        await scalar(
            engine,
            "SELECT count(*) FROM system_events WHERE event_type = 'reconciliation.discrepancy'",
        )
        == 1
    )


# --- #12: unknown order on the venue -----------------------------------------------


async def test_12_unknown_venue_order_is_a_discrepancy(
    engine: AsyncEngine, uow: UowFactory
) -> None:
    session = await session_for(engine, uow, ManualClock(NOW), script={})
    stranger = ExchangeOrder(
        "binance",
        "ex-stranger",
        "manual-123",
        SYMBOL,
        Side.BUY,
        OrderType.LIMIT,
        S.OPEN,
        "NEW",
        Decimal(1),
        Decimal(0),
        Decimal(1),
        None,
        None,
        NOW,
    )

    async def open_orders(symbol: str | None = None) -> Sequence[ExchangeOrder]:
        return [stranger]

    session.broker.fetch_open_orders = open_orders  # type: ignore[method-assign]
    report = await session.reconciler.run()

    assert any("unknown open order" in d for d in report.discrepancies)
    assert await kill_switch_state(session) is KillSwitchState.RECOVERY_REVIEW


# --- #16: venue unreachable; escalation after the max age --------------------------


async def test_16_unreachable_venue_keeps_order_pending_then_escalates(
    engine: AsyncEngine, uow: UowFactory
) -> None:
    clock = ManualClock(NOW)
    session = await session_for(engine, uow, clock, fault=(venue.RequestTimeout("t"), "before"))

    async def unreachable(*_: Any) -> ExchangeOrder | None:
        raise venue.ExchangeUnavailable("maintenance")

    session.broker.fetch_order_by_client_id = unreachable  # type: ignore[method-assign]
    await session.run_cycle()
    assert (await only_order(uow)).status is S.SUBMISSION_UNKNOWN

    clock.advance(timedelta(minutes=14))
    still = await session.reconciler.run()
    assert (await only_order(uow)).status is S.SUBMISSION_UNKNOWN
    assert any("lookup failed" in p for p in still.pending)

    clock.advance(timedelta(minutes=2))
    escalated = await session.reconciler.run()
    assert (await only_order(uow)).status is S.RECONCILIATION_REQUIRED
    assert escalated.discrepancies
    assert await kill_switch_state(session) is KillSwitchState.RECOVERY_REVIEW


# --- operator resolution ---------------------------------------------------------


class TestOperatorResolution:
    async def escalated(self, engine: AsyncEngine, uow: UowFactory) -> tuple[PaperSession, Order]:
        clock = ManualClock(NOW)
        session = await session_for(engine, uow, clock, fault=(venue.RequestTimeout("t"), "before"))

        async def unreachable(*_: Any) -> ExchangeOrder | None:
            raise venue.ExchangeUnavailable("down")

        original = session.broker.fetch_order_by_client_id
        session.broker.fetch_order_by_client_id = unreachable  # type: ignore[method-assign]
        await session.run_cycle()
        clock.advance(timedelta(minutes=16))
        await session.reconciler.run()
        session.broker.fetch_order_by_client_id = original  # type: ignore[method-assign]
        return session, await only_order(uow)

    async def test_operator_can_close_an_order_the_venue_never_saw(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        session, order = await self.escalated(engine, uow)
        assert order.status is S.RECONCILIATION_REQUIRED

        resolved = await resolve_order(
            order=order,
            outcome=S.NOT_PLACED,
            operator="bijesh",
            note="venue history shows no such client id",
            execution=session.broker,
            uow_factory=uow,
            clock=ManualClock(NOW + timedelta(minutes=20)),
        )

        assert resolved.status is S.NOT_PLACED
        assert (await session.reconciler.run()).clean
        # Trading still needs an explicit re-arm after the review.
        assert await kill_switch_state(session) is KillSwitchState.RECOVERY_REVIEW

    async def test_operator_cannot_override_the_venue(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        session, order = await self.escalated(engine, uow)
        await session.broker.ensure_funded(Decimal(1000))
        await session.broker.submit_order(order.intent)  # the venue does have it after all

        with pytest.raises(ResolutionRefusedError, match="let reconciliation apply"):
            await resolve_order(
                order=order,
                outcome=S.NOT_PLACED,
                operator="bijesh",
                note="guess",
                execution=session.broker,
                uow_factory=uow,
                clock=ManualClock(NOW + timedelta(minutes=20)),
            )

    @pytest.mark.parametrize(
        ("operator", "outcome", "note", "message"),
        [
            ("system", S.NOT_PLACED, "x", "named operator"),
            ("bijesh", S.NOT_PLACED, " ", "note"),
            ("bijesh", S.FILLED, "x", "zero-fill outcome"),
        ],
    )
    async def test_refusals(
        self,
        engine: AsyncEngine,
        uow: UowFactory,
        operator: str,
        outcome: OrderStatus,
        note: str,
        message: str,
    ) -> None:
        session, order = await self.escalated(engine, uow)

        with pytest.raises(ResolutionRefusedError, match=message):
            await resolve_order(
                order=order,
                outcome=outcome,
                operator=operator,
                note=note,
                execution=session.broker,
                uow_factory=uow,
                clock=ManualClock(NOW + timedelta(minutes=20)),
            )

    async def test_only_unresolved_orders(self, engine: AsyncEngine, uow: UowFactory) -> None:
        session = await session_for(engine, uow, ManualClock(NOW))
        await session.run_cycle()
        filled = await only_order(uow)

        with pytest.raises(ResolutionRefusedError, match="only unknown"):
            await resolve_order(
                order=filled,
                outcome=S.CANCELED,
                operator="bijesh",
                note="x",
                execution=session.broker,
                uow_factory=uow,
                clock=ManualClock(NOW),
            )
