"""Risk decisions, the database-enforced risk gate, and the kill switch."""

from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from arbitron.application.kill_switch import INITIAL_REASON, KillSwitchService
from arbitron.application.risk_gate import apply_risk_decision
from arbitron.clocks import ManualClock
from arbitron.domain.enums import KillSwitchState, OrderStatus
from arbitron.domain.errors import InvalidTransitionError
from arbitron.domain.events import OrderEvent
from arbitron.domain.orders import Order
from arbitron.domain.risk import RiskDecision, RiskReason
from arbitron.risk.manager import decide
from tests.factories import SMA_CONFIG, T0, at, make_intent, make_signal, make_snapshot
from tests.integration.conftest import UowFactory
from tests.unit.test_risk_manager import POLICY, entry

S = OrderStatus


async def created_order(uow: UowFactory) -> Order:
    order = Order.new(entry())
    async with uow() as tx:
        await tx.strategy_configs.add_if_absent(SMA_CONFIG, created_at=T0)
        await tx.signals.add_if_absent(make_signal())
        await tx.orders.add_if_absent(order, OrderEvent(order.id, None, S.CREATED, T0, "c"))
        await tx.commit()
    return order


class TestApplyRiskDecision:
    async def test_approval_persists_decision_and_transition_together(
        self, uow: UowFactory
    ) -> None:
        order = await created_order(uow)
        decision = decide(order.intent, make_snapshot(), POLICY, now=at(1))
        assert decision.approved

        async with uow() as tx:
            approved = await apply_risk_decision(tx, order, decision, correlation_id="c1")
            await tx.commit()

        async with uow() as tx:
            assert await tx.orders.get(order.id) == approved
            assert await tx.risk_decisions.get_for_intent(order.id) == decision
            events = await tx.orders.list_events(order.id)
        assert approved.status is S.RISK_APPROVED
        assert events[-1].payload["policy_version"] == POLICY.version

    async def test_rejection_records_reasons(self, uow: UowFactory) -> None:
        order = await created_order(uow)
        decision = decide(order.intent, make_snapshot(kill_switch=None), POLICY, now=at(1))

        async with uow() as tx:
            rejected = await apply_risk_decision(tx, order, decision, correlation_id="c1")
            await tx.commit()
            events = await tx.orders.list_events(order.id)

        assert rejected.status is S.RISK_REJECTED
        assert rejected.is_terminal
        assert "kill_switch_not_armed" in str(events[-1].payload["reasons"])

    async def test_nothing_is_kept_if_not_committed(self, uow: UowFactory) -> None:
        order = await created_order(uow)
        decision = decide(order.intent, make_snapshot(), POLICY, now=at(1))
        async with uow() as tx:
            await apply_risk_decision(tx, order, decision, correlation_id="c1")
        async with uow() as tx:
            assert await tx.risk_decisions.get_for_intent(order.id) is None
            current = await tx.orders.get(order.id)
        assert current is not None
        assert current.status is S.CREATED

    async def test_decision_must_match_order(self, uow: UowFactory) -> None:
        order = await created_order(uow)
        other = decide(make_intent(attempt=9), make_snapshot(), POLICY, now=at(1))
        async with uow() as tx:
            with pytest.raises(ValueError, match="applied to order"):
                await apply_risk_decision(tx, order, other, correlation_id="c1")

    async def test_only_created_orders_are_decided(self, uow: UowFactory) -> None:
        order = await created_order(uow)
        decision = decide(order.intent, make_snapshot(), POLICY, now=at(1))
        async with uow() as tx:
            await apply_risk_decision(tx, order, decision, correlation_id="c1")
            await tx.commit()
        approved = order.transition(S.RISK_APPROVED, at=at(1))
        async with uow() as tx:
            with pytest.raises(ValueError, match="only for created orders"):
                await apply_risk_decision(tx, approved, decision, correlation_id="c2")

    async def test_second_decision_is_refused(self, uow: UowFactory) -> None:
        order = await created_order(uow)
        decision = decide(order.intent, make_snapshot(), POLICY, now=at(1))
        async with uow() as tx:
            await tx.risk_decisions.add(decision)
            await tx.commit()
        from arbitron.ports.repositories import ConflictingRecordError  # noqa: PLC0415

        async with uow() as tx:
            with pytest.raises(ConflictingRecordError):
                await apply_risk_decision(tx, order, decision, correlation_id="c1")


class TestDatabaseRiskGate:
    """The trigger enforces the gate even if application code is bypassed."""

    async def test_cannot_approve_without_decision(self, uow: UowFactory) -> None:
        order = await created_order(uow)
        approved = order.transition(S.RISK_APPROVED, at=at(1))
        async with uow() as tx:
            with pytest.raises(DBAPIError, match="without an approved risk decision"):
                await tx.orders.apply_transition(
                    order, approved, OrderEvent(order.id, S.CREATED, S.RISK_APPROVED, at(1), "c")
                )

    async def test_cannot_approve_with_a_rejecting_decision(
        self, uow: UowFactory, engine: AsyncEngine
    ) -> None:
        order = await created_order(uow)
        rejection = RiskDecision(order.id, (RiskReason.TRADING_DISABLED,), "v", at(1))
        async with uow() as tx:
            await tx.risk_decisions.add(rejection)
            await tx.commit()
        async with engine.begin() as conn:
            with pytest.raises(DBAPIError, match="without an approved risk decision"):
                await conn.execute(
                    text("UPDATE orders SET status = 'risk_approved' WHERE id = :id"),
                    {"id": order.id},
                )

    async def test_cannot_reject_without_rejecting_decision(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        order = await created_order(uow)
        async with engine.begin() as conn:
            with pytest.raises(DBAPIError, match="without a rejecting risk decision"):
                await conn.execute(
                    text("UPDATE orders SET status = 'risk_rejected' WHERE id = :id"),
                    {"id": order.id},
                )

    async def test_orders_must_be_inserted_as_created(self, engine: AsyncEngine) -> None:
        from sqlalchemy import insert  # noqa: PLC0415

        from arbitron.persistence.mapping import order_values  # noqa: PLC0415
        from arbitron.persistence.tables import OrderRow  # noqa: PLC0415

        # Bypass the repository (which also refuses this) and write directly.
        values = order_values(Order.new(entry())) | {"status": "risk_approved"}
        async with engine.begin() as conn:
            with pytest.raises(DBAPIError, match="must be inserted as created"):
                await conn.execute(insert(OrderRow).values(**values))

    async def test_new_reason_codes_are_accepted(self, uow: UowFactory) -> None:
        order = await created_order(uow)
        decision = RiskDecision(
            order.id,
            (
                RiskReason.PRICE_OUTSIDE_BAND,
                RiskReason.POSITION_POLICY,
                RiskReason.INSUFFICIENT_BALANCE,
            ),
            "v",
            at(1),
        )
        async with uow() as tx:
            await tx.risk_decisions.add(decision)
            await tx.commit()

    async def test_unknown_reason_codes_are_refused(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        order = await created_order(uow)
        async with engine.begin() as conn:
            with pytest.raises(IntegrityError, match="known_reasons"):
                await conn.execute(
                    text(
                        "INSERT INTO risk_decisions (intent_id, approved, reasons, "
                        "policy_version, decided_at, observations) "
                        "VALUES (:id, false, ARRAY['made_up'], 'v', now(), '{}')"
                    ),
                    {"id": order.id},
                )


class TestKillSwitchService:
    async def test_first_use_initializes_to_review_not_armed(
        self, uow: UowFactory, engine: AsyncEngine
    ) -> None:
        service = KillSwitchService(uow, ManualClock(T0))

        state = await service.ensure_initialized()

        assert state.state is KillSwitchState.RECOVERY_REVIEW
        assert not state.allows_new_orders
        assert state.reason == INITIAL_REASON
        assert await service.ensure_initialized() == state  # idempotent

    async def test_operator_arms_then_trip_then_review_then_rearm(
        self, uow: UowFactory, engine: AsyncEngine
    ) -> None:
        clock = ManualClock(T0)
        service = KillSwitchService(uow, clock)

        clock.advance(timedelta(minutes=1))
        armed = await service.arm(operator="bijesh", acknowledgement="paper config reviewed")
        clock.advance(timedelta(minutes=1))
        tripped = await service.trip(reason="daily loss limit", actor="system")
        with pytest.raises(InvalidTransitionError):
            await service.arm(operator="bijesh", acknowledgement="skip review")
        clock.advance(timedelta(minutes=1))
        await service.begin_review(reason="investigating", actor="bijesh")
        clock.advance(timedelta(minutes=1))
        rearmed = await service.arm(operator="bijesh", acknowledgement="loss explained")

        assert armed.allows_new_orders
        assert tripped.state is KillSwitchState.TRIPPED
        assert rearmed.actor == "bijesh"
        async with engine.connect() as conn:
            history = (
                (await conn.execute(text("SELECT to_state FROM kill_switch_events ORDER BY id")))
                .scalars()
                .all()
            )
            events = (
                (await conn.execute(text("SELECT event_type FROM system_events ORDER BY id")))
                .scalars()
                .all()
            )
        assert history == ["recovery_review", "armed", "tripped", "recovery_review", "armed"]
        assert events == [
            "kill_switch.initialized",
            "kill_switch.armed",
            "kill_switch.tripped",
            "kill_switch.review_started",
            "kill_switch.armed",
        ]

    async def test_system_cannot_arm(self, uow: UowFactory) -> None:
        service = KillSwitchService(uow, ManualClock(T0))

        with pytest.raises(InvalidTransitionError, match="named operator"):
            await service.arm(operator="system", acknowledgement="auto")

    async def test_trip_works_from_any_state(self, uow: UowFactory) -> None:
        service = KillSwitchService(uow, ManualClock(T0))

        state = await service.trip(reason="operator emergency", actor="bijesh")

        assert state.state is KillSwitchState.TRIPPED
