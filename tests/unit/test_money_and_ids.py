from decimal import Decimal
from uuid import UUID

import pytest

from arbitron.domain.errors import DomainValidationError
from arbitron.domain.ids import (
    client_order_id_for,
    deterministic_id,
    is_canonical_client_order_id,
)
from arbitron.domain.money import is_multiple_of_step, round_down_to_step, to_decimal


class TestToDecimal:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("0.1", Decimal("0.1")), (5, Decimal(5)), (Decimal("1.50"), Decimal("1.50"))],
    )
    def test_accepts_exact_inputs(self, raw: object, expected: Decimal) -> None:
        assert to_decimal(raw) == expected

    def test_preserves_exact_text(self) -> None:
        assert str(to_decimal("0.10000000000000000001")) == "0.10000000000000000001"

    @pytest.mark.parametrize("raw", [0.1, True, None, [1]])
    def test_refuses_float_bool_and_other_types(self, raw: object) -> None:
        with pytest.raises(DomainValidationError, match="cannot convert"):
            to_decimal(raw)

    @pytest.mark.parametrize("raw", ["abc", "NaN", "Infinity", "-inf"])
    def test_refuses_invalid_and_non_finite(self, raw: str) -> None:
        with pytest.raises(DomainValidationError):
            to_decimal(raw)


class TestRoundDownToStep:
    @pytest.mark.parametrize(
        ("value", "step", "expected"),
        [
            ("0.123456", "0.001", "0.123"),
            ("0.999999", "0.001", "0.999"),
            ("5", "0.001", "5.000"),
            ("0.0009", "0.001", "0.000"),
            ("1234.5", "10", "1230"),
            ("0.37", "0.05", "0.35"),
            (
                "12345678901234567890.123456789012345678",
                "0.000000000000000001",
                "12345678901234567890.123456789012345678",
            ),
        ],
    )
    def test_rounds_down(self, value: str, step: str, expected: str) -> None:
        result = round_down_to_step(Decimal(value), Decimal(step))

        assert result == Decimal(expected)
        assert result <= Decimal(value)
        assert is_multiple_of_step(result, Decimal(step))

    def test_result_uses_step_exponent(self) -> None:
        assert str(round_down_to_step(Decimal(5), Decimal("0.001"))) == "5.000"

    @pytest.mark.parametrize("step", ["0", "-0.1"])
    def test_step_must_be_positive(self, step: str) -> None:
        with pytest.raises(DomainValidationError):
            round_down_to_step(Decimal(1), Decimal(step))

    def test_negative_value_is_refused(self) -> None:
        with pytest.raises(DomainValidationError, match="must not be negative"):
            round_down_to_step(Decimal("-1"), Decimal("0.1"))

    def test_is_multiple_of_step(self) -> None:
        assert is_multiple_of_step(Decimal("0.35"), Decimal("0.05"))
        assert not is_multiple_of_step(Decimal("0.37"), Decimal("0.05"))


class TestIds:
    def test_deterministic_id_is_stable_and_kind_scoped(self) -> None:
        assert deterministic_id("signal", "k") == deterministic_id("signal", "k")
        assert deterministic_id("signal", "k") != deterministic_id("order_intent", "k")
        assert deterministic_id("signal", "k").version == 5

    def test_client_order_id_format(self) -> None:
        client_id = client_order_id_for(UUID(int=0))

        assert client_id == "arb" + "0" * 29
        assert is_canonical_client_order_id(client_id)

    @pytest.mark.parametrize("value", ["", "arb", "ARB" + "0" * 29, "arb" + "g" * 29, "x" * 32])
    def test_non_canonical_client_ids(self, value: str) -> None:
        assert not is_canonical_client_order_id(value)
