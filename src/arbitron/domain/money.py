"""Decimal helpers for prices, quantities, fees, and balances.

All financial arithmetic uses ``Decimal`` with an explicit, wide context so
values up to NUMERIC(38, 18) are represented without silent rounding.
"""

from decimal import (
    ROUND_DOWN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)

from arbitron.domain._checks import require_decimal, require_positive
from arbitron.domain.errors import DomainValidationError

# 60 significant digits comfortably covers NUMERIC(38, 18) operands and their
# products; traps turn silent NaN/overflow into exceptions.
FINANCIAL_CONTEXT = Context(prec=60, traps=[InvalidOperation, DivisionByZero, Overflow])


def to_decimal(value: object) -> Decimal:
    """Convert an exchange or config value to Decimal, refusing binary floats.

    Accepts ``str``, ``int``, or ``Decimal``; the parameter is typed ``object``
    because callers pass untyped exchange payload values. Use string inputs for
    fractional values so the exact decimal text is kept.
    """
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        msg = f"cannot convert {type(value).__name__} to Decimal; pass str, int, or Decimal"
        raise DomainValidationError(msg)
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        msg = f"invalid decimal value {value!r}"
        raise DomainValidationError(msg) from exc
    return require_decimal("value", result)


def round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round a non-negative value down to a multiple of ``step``.

    Used for exchange amount/price precision: rounding down never produces an
    order larger than the sizer intended.
    """
    require_decimal("value", value)
    require_positive("step", step)
    if value < 0:
        msg = f"value must not be negative, got {value}"
        raise DomainValidationError(msg)
    with localcontext(FINANCIAL_CONTEXT):
        steps = (value / step).to_integral_value(rounding=ROUND_DOWN)
        return (steps * step).quantize(step)


def is_multiple_of_step(value: Decimal, step: Decimal) -> bool:
    require_decimal("value", value)
    require_positive("step", step)
    with localcontext(FINANCIAL_CONTEXT):
        return value % step == 0
