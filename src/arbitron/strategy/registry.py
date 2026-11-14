"""Known strategies, and building one from configuration."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from arbitron.domain.strategies import ParameterValue
from arbitron.strategy.base import Strategy
from arbitron.strategy.implementations import (
    breakout,
    buy_hold,
    mean_reversion,
    sma_cross,
    trend_filter,
)


class UnknownStrategyError(LookupError):
    pass


class StrategyConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RegisteredStrategy:
    strategy_id: str
    version: str
    factory: Callable[[Mapping[str, ParameterValue]], Strategy]
    allowed_in_live: bool


REGISTRY: Mapping[str, RegisteredStrategy] = {
    sma_cross.STRATEGY_ID: RegisteredStrategy(
        strategy_id=sma_cross.STRATEGY_ID,
        version=sma_cross.VERSION,
        factory=sma_cross.SmaCross.from_parameters,
        allowed_in_live=sma_cross.SmaCross.allowed_in_live,
    ),
    buy_hold.STRATEGY_ID: RegisteredStrategy(
        strategy_id=buy_hold.STRATEGY_ID,
        version=buy_hold.VERSION,
        factory=buy_hold.BuyHold.from_parameters,
        allowed_in_live=buy_hold.BuyHold.allowed_in_live,
    ),
    trend_filter.STRATEGY_ID: RegisteredStrategy(
        strategy_id=trend_filter.STRATEGY_ID,
        version=trend_filter.VERSION,
        factory=trend_filter.TrendFilter.from_parameters,
        allowed_in_live=trend_filter.TrendFilter.allowed_in_live,
    ),
    breakout.STRATEGY_ID: RegisteredStrategy(
        strategy_id=breakout.STRATEGY_ID,
        version=breakout.VERSION,
        factory=breakout.Breakout.from_parameters,
        allowed_in_live=breakout.Breakout.allowed_in_live,
    ),
    mean_reversion.STRATEGY_ID: RegisteredStrategy(
        strategy_id=mean_reversion.STRATEGY_ID,
        version=mean_reversion.VERSION,
        factory=mean_reversion.MeanReversion.from_parameters,
        allowed_in_live=mean_reversion.MeanReversion.allowed_in_live,
    ),
}


def build_strategy(
    strategy_id: str,
    version: str,
    parameters: Mapping[str, ParameterValue],
    *,
    live: bool = False,
) -> Strategy:
    """Instantiate a configured strategy, refusing anything that does not match the code.

    The configured ``version`` must equal the implementation's version, so a
    config written for older behavior can never silently run newer code under
    the old label.
    """
    entry = REGISTRY.get(strategy_id)
    if entry is None:
        msg = f"unknown strategy {strategy_id!r} (known: {sorted(REGISTRY)})"
        raise UnknownStrategyError(msg)
    if version != entry.version:
        msg = (
            f"strategy {strategy_id!r} is at version {entry.version!r} but the config "
            f"names version {version!r}; review the change and update the config"
        )
        raise StrategyConfigError(msg)
    if live and not entry.allowed_in_live:
        msg = f"strategy {strategy_id!r} is not allowed in live mode"
        raise StrategyConfigError(msg)
    try:
        return entry.factory(parameters)
    except ValueError as exc:
        msg = f"invalid parameters for {strategy_id!r}: {exc}"
        raise StrategyConfigError(msg) from exc
