"""Helpers shared by the research strategies: parameter parsing and indicators."""

import itertools
from collections.abc import Mapping, Sequence
from decimal import Decimal, localcontext

from arbitron.domain.money import FINANCIAL_CONTEXT
from arbitron.domain.strategies import ParameterValue


class StrategyParameterError(ValueError):
    pass


def int_parameters(
    strategy_id: str, parameters: Mapping[str, ParameterValue], names: Sequence[str]
) -> dict[str, int]:
    """Exactly ``names``, each a plain integer (bool is an int subclass, so refused)."""
    unknown = set(parameters) - set(names)
    if unknown:
        msg = f"unknown {strategy_id} parameters: {sorted(unknown)}"
        raise StrategyParameterError(msg)
    missing = [name for name in names if name not in parameters]
    if missing:
        msg = f"{strategy_id} requires integer parameters {list(names)}; missing {missing}"
        raise StrategyParameterError(msg)
    values: dict[str, int] = {}
    for name in names:
        value = parameters[name]
        if isinstance(value, bool | str | float):
            msg = f"{strategy_id} parameter {name!r} must be an integer, got {value!r}"
            raise StrategyParameterError(msg)
        values[name] = value
    return values


def positive(name: str, value: int) -> None:
    if value < 1:
        msg = f"{name} must be a positive integer, got {value!r}"
        raise StrategyParameterError(msg)


def sma(values: Sequence[Decimal], length: int) -> Decimal:
    """Mean of the last ``length`` values."""
    with localcontext(FINANCIAL_CONTEXT):
        return sum(values[-length:], Decimal(0)) / length


def rsi(closes: Sequence[Decimal], length: int) -> Decimal:
    """Wilder's RSI over ``closes`` (needs at least ``length + 1`` values), 0 to 100."""
    with localcontext(FINANCIAL_CONTEXT):
        changes = [b - a for a, b in itertools.pairwise(closes)]
        gains = [max(c, Decimal(0)) for c in changes]
        losses = [max(-c, Decimal(0)) for c in changes]
        avg_gain = sum(gains[:length], Decimal(0)) / length
        avg_loss = sum(losses[:length], Decimal(0)) / length
        for gain, loss in zip(gains[length:], losses[length:], strict=True):
            avg_gain = (avg_gain * (length - 1) + gain) / length
            avg_loss = (avg_loss * (length - 1) + loss) / length
        if avg_loss == 0:
            return Decimal(100) if avg_gain > 0 else Decimal(50)
        return Decimal(100) - Decimal(100) / (1 + avg_gain / avg_loss)
