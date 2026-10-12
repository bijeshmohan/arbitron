"""Decimal helpers for prices, quantities, fees, and balances.

All financial arithmetic uses ``Decimal`` with an explicit, wide context so
values up to NUMERIC(38, 18) are represented without silent rounding.
"""

from decimal import (
    ROUND_DOWN,
    ROUND_UP,
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


def round_up_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round a non-negative value up to a multiple of ``step``.

    Used for SELL limit prices: rounding a price floor up never sells below it.
    """
    require_decimal("value", value)
    require_positive("step", step)
    if value < 0:
        msg = f"value must not be negative, got {value}"
        raise DomainValidationError(msg)
    with localcontext(FINANCIAL_CONTEXT):
        steps = (value / step).to_integral_value(rounding=ROUND_UP)
        return (steps * step).quantize(step)


def is_multiple_of_step(value: Decimal, step: Decimal) -> bool:
    require_decimal("value", value)
    require_positive("step", step)
    with localcontext(FINANCIAL_CONTEXT):
        return value % step == 0


def canonical_str(value: Decimal) -> str:
    """Plain decimal text without exponent or trailing zeros: ``12.500`` -> ``12.5``.

    Numerically equal Decimals can carry different exponents depending on their
    source (``NUMERIC(38, 18)`` returns ``12.500000000000000000``). Use this
    whenever a Decimal becomes part of persisted or compared text, so the text
    depends only on the value.
    """
    require_decimal("value", value)
    # normalize() rounds to the context precision; give it exactly as many
    # digits as the value has so nothing is ever rounded away.
    exact = Context(prec=max(len(value.as_tuple().digits), 1))
    text = f"{value.normalize(exact):f}"
    return "0" if text == "-0" else text
