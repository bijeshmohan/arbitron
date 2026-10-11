from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from arbitron.domain.enums import OrderStatus, OrderType, Side
from arbitron.domain.errors import DomainValidationError, InvalidTransitionError
from arbitron.domain.ids import is_canonical_client_order_id
from arbitron.domain.orders import ExchangeOrder, Order
from tests.factories import T0, at, make_intent, make_order

S = OrderStatus


class TestOrderIntent:
    def test_id_and_client_order_id_are_deterministic(self) -> None:
        first, second = make_intent(), make_intent()

        assert first.id == second.id
        assert first.client_order_id == second.client_order_id
        assert is_canonical_client_order_id(first.client_order_id)
        assert len(first.client_order_id) == 32

    def test_new_attempt_gets_new_ids(self) -> None:
        original, retry = make_intent(), make_intent(attempt=2)

        assert original.id != retry.id
        assert original.client_order_id != retry.client_order_id

    def test_different_signals_get_different_ids(self) -> None:
        other = make_intent(signal_id=make_intent().id)  # any other UUID

        assert other.id != make_intent().id

    @pytest.mark.parametrize("amount", [Decimal(0), Decimal("-0.1"), Decimal("NaN")])
    def test_amount_must_be_positive_and_finite(self, amount: Decimal) -> None:
        with pytest.raises(DomainValidationError):
            make_intent(amount=amount)

    def test_float_amount_is_refused(self) -> None:
        with pytest.raises(DomainValidationError, match="must be a Decimal"):
            make_intent(amount=0.01)

    def test_limit_order_requires_price(self) -> None:
        with pytest.raises(DomainValidationError, match="require a limit_price"):
            make_intent(limit_price=None)

    def test_market_order_must_not_have_price(self) -> None:
        with pytest.raises(DomainValidationError, match="must not carry a limit_price"):
            make_intent(order_type=OrderType.MARKET)

    def test_market_order_without_price_is_valid(self) -> None:
        intent = make_intent(order_type=OrderType.MARKET, limit_price=None)

        assert intent.limit_price is None

    def test_naive_datetime_is_refused(self) -> None:
        with pytest.raises(DomainValidationError, match="timezone-aware UTC"):
            make_intent(created_at=datetime(2026, 1, 1))  # noqa: DTZ001

    def test_non_utc_offset_is_refused(self) -> None:
        ist = timezone(timedelta(hours=5, minutes=30))
        with pytest.raises(DomainValidationError, match="timezone-aware UTC"):
            make_intent(created_at=datetime(2026, 1, 1, tzinfo=ist))

    def test_attempt_must_be_positive(self) -> None:
        with pytest.raises(DomainValidationError, match="attempt"):
            make_intent(attempt=0)

    def test_sizing_inputs_are_immutable(self) -> None:
        inputs = {"equity": "1000"}
        intent = make_intent(sizing_inputs=inputs)
        inputs["equity"] = "999999"

        assert intent.sizing_inputs["equity"] == "1000"
        with pytest.raises(TypeError):
            intent.sizing_inputs["equity"] = "1"  # type: ignore[index]


class TestOrderInvariants:
    def test_new_order_starts_created_with_no_fill(self) -> None:
        intent = make_intent()
        order = Order.new(intent)

        assert order.status is S.CREATED
        assert order.filled_amount == 0
        assert order.id == intent.id
        assert order.updated_at == intent.created_at

    @pytest.mark.parametrize(
        "status",
        [S.CREATED, S.RISK_APPROVED, S.SUBMITTING, S.OPEN, S.CANCELED, S.EXPIRED, S.NOT_PLACED],
    )
    def test_zero_fill_statuses_reject_fills(self, status: OrderStatus) -> None:
        with pytest.raises(DomainValidationError, match="zero filled_amount"):
            make_order(status, filled_amount=Decimal("0.001"), average_fill_price=Decimal(1))

    @pytest.mark.parametrize("status", [S.PARTIALLY_FILLED, S.PARTIALLY_FILLED_CLOSED])
    @pytest.mark.parametrize("filled", [Decimal(0), Decimal("0.010")])
    def test_partial_statuses_require_strict_partial_fill(
        self, status: OrderStatus, filled: Decimal
    ) -> None:
        with pytest.raises(DomainValidationError, match="0 < filled_amount < amount"):
            make_order(
                status, filled_amount=filled, average_fill_price=Decimal(1) if filled else None
            )

    def test_filled_requires_full_amount(self) -> None:
        with pytest.raises(DomainValidationError, match="filled_amount == amount"):
            make_order(S.FILLED, filled_amount=Decimal("0.009"))

    def test_fill_cannot_exceed_amount(self) -> None:
        with pytest.raises(DomainValidationError, match="exceeds order amount"):
            make_order(S.FILLED, filled_amount=Decimal("0.011"))

    def test_cancel_requested_cannot_be_fully_filled(self) -> None:
        with pytest.raises(DomainValidationError, match="cancel_requested"):
            make_order(
                S.CANCEL_REQUESTED,
                filled_amount=Decimal("0.010"),
                average_fill_price=Decimal(1),
            )

    def test_pre_submission_order_cannot_have_exchange_id(self) -> None:
        with pytest.raises(DomainValidationError, match="cannot have an exchange_order_id"):
            make_order(S.SUBMITTING, exchange_order_id="ex-1")

    def test_average_price_requires_fill(self) -> None:
        with pytest.raises(DomainValidationError, match="requires a non-zero filled_amount"):
            make_order(S.OPEN, average_fill_price=Decimal(1))


class TestOrderTransitions:
    def test_fill_cannot_decrease(self) -> None:
        order = make_order(S.PARTIALLY_FILLED)

        with pytest.raises(InvalidTransitionError, match="cannot decrease"):
            order.transition(S.PARTIALLY_FILLED, at=at(7), filled_amount=Decimal("0.003"))

    def test_more_partial_fills_are_recorded(self) -> None:
        order = make_order(S.PARTIALLY_FILLED).transition(
            S.PARTIALLY_FILLED, at=at(7), filled_amount=Decimal("0.006")
        )

        assert order.filled_amount == Decimal("0.006")
        assert order.remaining_amount == Decimal("0.004")

    def test_exchange_order_id_cannot_change(self) -> None:
        with pytest.raises(InvalidTransitionError, match="exchange_order_id cannot change"):
            make_order(S.OPEN).transition(S.CANCELED, at=at(7), exchange_order_id="ex-2")

    def test_time_cannot_move_backwards(self) -> None:
        with pytest.raises(InvalidTransitionError, match="precedes last update"):
            make_order(S.OPEN).transition(S.CANCELED, at=T0)

    def test_invariant_violation_is_reported_as_invalid_transition(self) -> None:
        # OPEN -> FILLED is allowed, but not without the full fill.
        with pytest.raises(InvalidTransitionError, match="violates invariants"):
            make_order(S.OPEN).transition(S.FILLED, at=at(7), filled_amount=Decimal("0.005"))

    def test_fill_racing_cancel_is_recorded_in_cancel_requested(self) -> None:
        order = make_order(S.OPEN).transition(S.CANCEL_REQUESTED, at=at(7))
        order = order.transition(
            S.CANCEL_REQUESTED,
            at=at(8),
            filled_amount=Decimal("0.002"),
            average_fill_price=Decimal("49995"),
        )
        order = order.transition(S.PARTIALLY_FILLED_CLOSED, at=at(9))

        assert order.status is S.PARTIALLY_FILLED_CLOSED
        assert order.filled_amount == Decimal("0.002")

    def test_cancel_racing_complete_fill_ends_filled(self) -> None:
        order = make_order(S.OPEN).transition(S.CANCEL_REQUESTED, at=at(7))

        order = order.transition(S.FILLED, at=at(8), filled_amount=Decimal("0.010"))

        assert order.status is S.FILLED

    def test_unknown_submission_resolves_to_not_placed(self) -> None:
        order = make_order(S.SUBMITTING).transition(S.SUBMISSION_UNKNOWN, at=at(7))

        order = order.transition(S.NOT_PLACED, at=at(8))

        assert order.is_terminal
        assert order.exchange_order_id is None

    def test_immediate_or_cancel_can_close_on_submission(self) -> None:
        order = make_order(S.SUBMITTING).transition(S.EXPIRED, at=at(7), exchange_order_id="ex-9")

        assert order.status is S.EXPIRED


class TestExchangeOrder:
    def _make(self, **overrides: object) -> ExchangeOrder:
        fields: dict[str, object] = {
            "exchange_id": "binance",
            "exchange_order_id": "ex-1",
            "client_order_id": make_intent().client_order_id,
            "symbol": "BTC/USDT",
            "side": Side.BUY,
            "order_type": OrderType.LIMIT,
            "status": S.OPEN,
            "raw_status": "NEW",
            "amount": Decimal("0.010"),
            "filled": Decimal(0),
            "remaining": Decimal("0.010"),
            "average_price": None,
            "fee": None,
            "timestamp": at(6),
        }
        fields.update(overrides)
        return ExchangeOrder(**fields)  # type: ignore[arg-type]

    def test_valid_snapshot(self) -> None:
        assert self._make().raw_status == "NEW"

    @pytest.mark.parametrize(
        "status",
        [S.CREATED, S.SUBMITTING, S.SUBMISSION_UNKNOWN, S.CANCEL_REQUESTED, S.NOT_PLACED],
    )
    def test_local_statuses_cannot_come_from_venue(self, status: OrderStatus) -> None:
        with pytest.raises(DomainValidationError, match="local status"):
            self._make(status=status)

    def test_filled_cannot_exceed_amount(self) -> None:
        with pytest.raises(DomainValidationError, match="exceeds amount"):
            self._make(filled=Decimal("0.011"))


def test_orders_are_immutable() -> None:
    order = make_order()

    with pytest.raises(AttributeError):
        order.status = S.FILLED  # type: ignore[misc]
    assert replace(order).status is S.CREATED
