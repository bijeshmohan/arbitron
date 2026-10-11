import itertools

import pytest

from arbitron.domain.enums import KillSwitchState
from arbitron.domain.errors import DomainValidationError, InvalidTransitionError
from arbitron.domain.risk import KillSwitch, RiskDecision, RiskReason
from tests.factories import T0, at, make_intent

K = KillSwitchState

EXPECTED_KILL_SWITCH: dict[KillSwitchState, set[KillSwitchState]] = {
    K.ARMED: {K.TRIPPED, K.RECOVERY_REVIEW},
    K.TRIPPED: {K.TRIPPED, K.RECOVERY_REVIEW},
    K.RECOVERY_REVIEW: {K.ARMED, K.TRIPPED},
}


def switch(state: KillSwitchState) -> KillSwitch:
    return KillSwitch(state=state, changed_at=T0, actor="system", reason="initial")


def move(ks: KillSwitch, target: KillSwitchState) -> KillSwitch:
    if target is K.TRIPPED:
        return ks.trip(at=at(1), reason="test")
    if target is K.RECOVERY_REVIEW:
        return ks.begin_review(at=at(1), reason="test")
    return ks.rearm(at=at(1), operator="alice", acknowledgement="reviewed")


class TestRiskDecision:
    def test_approved_iff_no_reasons(self) -> None:
        approved = RiskDecision(make_intent().id, (), "risk-v1", T0)
        rejected = RiskDecision(make_intent().id, (RiskReason.MAX_ORDER_NOTIONAL,), "risk-v1", T0)

        assert approved.approved
        assert not rejected.approved

    def test_duplicate_reasons_refused(self) -> None:
        with pytest.raises(DomainValidationError, match="unique"):
            RiskDecision(
                make_intent().id,
                (RiskReason.MAX_OPEN_ORDERS, RiskReason.MAX_OPEN_ORDERS),
                "risk-v1",
                T0,
            )

    def test_observations_are_read_only(self) -> None:
        decision = RiskDecision(make_intent().id, (), "risk-v1", T0, {"order_notional": "49.99"})

        with pytest.raises(TypeError):
            decision.observations["order_notional"] = "0"  # type: ignore[index]


class TestKillSwitch:
    @pytest.mark.parametrize(
        ("current", "target"), list(itertools.product(KillSwitchState, KillSwitchState))
    )
    def test_transition_table(self, current: KillSwitchState, target: KillSwitchState) -> None:
        if target in EXPECTED_KILL_SWITCH[current]:
            assert move(switch(current), target).state is target
        else:
            with pytest.raises(InvalidTransitionError):
                move(switch(current), target)

    @pytest.mark.parametrize("state", list(KillSwitchState))
    def test_only_armed_allows_new_orders(self, state: KillSwitchState) -> None:
        assert switch(state).allows_new_orders is (state is K.ARMED)

    def test_tripped_cannot_rearm_without_review(self) -> None:
        with pytest.raises(InvalidTransitionError, match="tripped -> armed"):
            switch(K.TRIPPED).rearm(at=at(1), operator="alice", acknowledgement="ok")

    def test_full_recovery_records_operator(self) -> None:
        ks = switch(K.ARMED).trip(at=at(1), reason="daily loss limit")
        ks = ks.begin_review(at=at(2), reason="awaiting operator")
        ks = ks.rearm(at=at(3), operator="alice", acknowledgement="loss explained; limits ok")

        assert ks.allows_new_orders
        assert ks.actor == "alice"
        assert ks.reason == "loss explained; limits ok"

    @pytest.mark.parametrize("operator", ["system", " system ", ""])
    def test_system_or_blank_operator_cannot_rearm(self, operator: str) -> None:
        with pytest.raises((InvalidTransitionError, DomainValidationError)):
            switch(K.RECOVERY_REVIEW).rearm(at=at(1), operator=operator, acknowledgement="ok")

    def test_rearm_requires_acknowledgement_text(self) -> None:
        with pytest.raises(DomainValidationError, match="reason"):
            switch(K.RECOVERY_REVIEW).rearm(at=at(1), operator="alice", acknowledgement=" ")

    def test_time_cannot_move_backwards(self) -> None:
        ks = KillSwitch(K.ARMED, at(10), "system", "initial")

        with pytest.raises(InvalidTransitionError, match="precedes"):
            ks.trip(at=at(5), reason="late")
