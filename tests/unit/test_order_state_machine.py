"""Exhaustive checks of the order state machine against plan §6.2.

The expected table below is re-typed from the plan rather than derived from
the implementation, so a change to either one fails these tests.
"""

import itertools
from decimal import Decimal

import pytest

from arbitron.domain.enums import OrderStatus
from arbitron.domain.errors import InvalidTransitionError
from arbitron.domain.orders import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    can_transition,
    is_terminal,
)
from tests.factories import at, make_order

S = OrderStatus
RR = S.RECONCILIATION_REQUIRED

EXCHANGE_OUTCOMES = {
    S.OPEN,
    S.PARTIALLY_FILLED,
    S.FILLED,
    S.CANCELED,
    S.EXPIRED,
    S.PARTIALLY_FILLED_CLOSED,
    S.EXCHANGE_REJECTED,
    S.NOT_PLACED,
}

EXPECTED: dict[OrderStatus, set[OrderStatus]] = {
    S.CREATED: {S.RISK_APPROVED, S.RISK_REJECTED, RR},
    S.RISK_APPROVED: {S.SUBMITTING, RR},
    S.SUBMITTING: {
        S.OPEN,
        S.PARTIALLY_FILLED,
        S.FILLED,
        S.CANCELED,
        S.EXPIRED,
        S.PARTIALLY_FILLED_CLOSED,
        S.EXCHANGE_REJECTED,
        S.SUBMISSION_UNKNOWN,
        RR,
    },
    S.OPEN: {
        S.PARTIALLY_FILLED,
        S.FILLED,
        S.CANCEL_REQUESTED,
        S.CANCELED,
        S.EXPIRED,
        S.PARTIALLY_FILLED_CLOSED,
        RR,
    },
    S.PARTIALLY_FILLED: {
        S.PARTIALLY_FILLED,
        S.FILLED,
        S.CANCEL_REQUESTED,
        S.PARTIALLY_FILLED_CLOSED,
        RR,
    },
    S.CANCEL_REQUESTED: {
        S.CANCEL_REQUESTED,
        S.OPEN,
        S.PARTIALLY_FILLED,
        S.FILLED,
        S.CANCELED,
        S.EXPIRED,
        S.PARTIALLY_FILLED_CLOSED,
        RR,
    },
    S.SUBMISSION_UNKNOWN: EXCHANGE_OUTCOMES | {RR},
    RR: EXCHANGE_OUTCOMES,
    S.RISK_REJECTED: set(),
    S.EXCHANGE_REJECTED: set(),
    S.NOT_PLACED: set(),
    S.FILLED: set(),
    S.CANCELED: set(),
    S.EXPIRED: set(),
    S.PARTIALLY_FILLED_CLOSED: set(),
}


def test_every_status_has_an_expectation() -> None:
    assert set(EXPECTED) == set(OrderStatus)
    assert set(ALLOWED_TRANSITIONS) == set(OrderStatus)


@pytest.mark.parametrize(("current", "target"), list(itertools.product(OrderStatus, OrderStatus)))
def test_transition_table_matches_plan(current: OrderStatus, target: OrderStatus) -> None:
    assert can_transition(current, target) is (target in EXPECTED[current])


def test_terminal_statuses_are_exactly_those_without_exits() -> None:
    assert {s for s, exits in EXPECTED.items() if not exits} == set(TERMINAL_STATUSES)
    assert all(is_terminal(s) for s in TERMINAL_STATUSES)


@pytest.mark.parametrize("status", [S.SUBMISSION_UNKNOWN, RR])
def test_ambiguous_states_are_not_terminal(status: OrderStatus) -> None:
    assert not is_terminal(status)


@pytest.mark.parametrize("status", sorted(set(OrderStatus) - set(TERMINAL_STATUSES) - {RR}))
def test_any_non_terminal_state_can_escalate_to_reconciliation(status: OrderStatus) -> None:
    assert can_transition(status, RR)


@pytest.mark.parametrize("target", [S.CREATED, S.RISK_APPROVED, S.RISK_REJECTED, S.SUBMITTING])
def test_reconciliation_never_returns_to_pre_submission(target: OrderStatus) -> None:
    assert not can_transition(RR, target)
    assert not can_transition(S.SUBMISSION_UNKNOWN, target)


def test_order_walks_happy_path() -> None:
    order = make_order()
    order = order.transition(S.RISK_APPROVED, at=at(7))
    order = order.transition(S.SUBMITTING, at=at(8))
    order = order.transition(S.OPEN, at=at(9), exchange_order_id="ex-1")
    order = order.transition(
        S.PARTIALLY_FILLED,
        at=at(10),
        filled_amount=Decimal("0.004"),
        average_fill_price=Decimal("49990"),
    )
    order = order.transition(S.FILLED, at=at(11), filled_amount=Decimal("0.010"))

    assert order.status is S.FILLED
    assert order.is_terminal
    assert order.remaining_amount == 0
    assert order.exchange_order_id == "ex-1"


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
def test_terminal_order_rejects_every_transition(status: OrderStatus) -> None:
    order = make_order(status)

    for target in OrderStatus:
        with pytest.raises(InvalidTransitionError):
            order.transition(target, at=at(30))


def test_disallowed_transition_raises() -> None:
    with pytest.raises(InvalidTransitionError, match="created -> submitting is not allowed"):
        make_order().transition(S.SUBMITTING, at=at(7))
