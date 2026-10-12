"""Turning a strategy plus candles into signals: one shared path for live and replay.

The live runner and ``replay`` both call ``evaluate_candle``, so a replay over
stored candles reproduces exactly what the live runner emitted (apart from
``created_at``, which records wall-clock processing time).
"""

import itertools
from collections.abc import Iterator, Sequence
from datetime import datetime

from arbitron.domain.errors import DomainError
from arbitron.domain.market import Candle, Timeframe
from arbitron.domain.portfolio import PortfolioView
from arbitron.domain.signals import Signal
from arbitron.domain.strategies import StrategyConfig
from arbitron.strategy.base import Strategy, StrategyContext, StrategyError


def config_of(strategy: Strategy) -> StrategyConfig:
    return StrategyConfig(strategy.strategy_id, strategy.version, strategy.parameters)


def window_problem(window: Sequence[Candle], timeframe: Timeframe, lookback: int) -> str | None:
    """Why ``window`` is not a valid evaluation window, or ``None`` if it is."""
    if len(window) != lookback:
        return f"window has {len(window)} candles, strategy needs {lookback}"
    if any(candle.timeframe != timeframe for candle in window):
        return f"window contains candles that are not {timeframe}"
    for previous, candle in itertools.pairwise(window):
        if candle.open_time - previous.open_time != timeframe.duration:
            return f"window is not contiguous at {candle.open_time.isoformat()}"
    if not all(candle.is_closed for candle in window):
        return "window contains an unfinished candle"
    return None


def evaluate_candle(
    strategy: Strategy,
    *,
    window: Sequence[Candle],
    timeframe: Timeframe,
    portfolio: PortfolioView,
    created_at: datetime,
) -> Signal | None:
    """Evaluate the last candle of ``window``. Raises ``StrategyError`` on contract breaks."""
    problem = window_problem(window, timeframe, strategy.lookback)
    if problem is not None:
        msg = f"{strategy.strategy_id}: invalid evaluation window: {problem}"
        raise StrategyError(msg)
    current = window[-1]
    context = StrategyContext(
        exchange_id=current.exchange_id,
        symbol=current.symbol,
        timeframe=timeframe,
        candles=tuple(window),
        portfolio=portfolio,
    )
    try:
        proposals = list(strategy.evaluate(context))
    except Exception as exc:
        msg = f"{strategy.strategy_id} raised while evaluating {current.open_time.isoformat()}"
        raise StrategyError(msg) from exc
    if len(proposals) > 1:
        msg = f"{strategy.strategy_id} returned {len(proposals)} proposals; at most 1 allowed"
        raise StrategyError(msg)
    if not proposals:
        return None
    proposal = proposals[0]
    try:
        return Signal(
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.version,
            config_hash=config_of(strategy).config_hash,
            exchange_id=current.exchange_id,
            symbol=current.symbol,
            timeframe=timeframe,
            side=proposal.side,
            source_candle_time=current.open_time,
            created_at=created_at,
            reason_code=proposal.reason_code,
            explanation=proposal.explanation,
            metadata=proposal.metadata,
        )
    except DomainError as exc:
        msg = f"{strategy.strategy_id} produced an invalid signal: {exc}"
        raise StrategyError(msg) from exc


def replay(
    strategy: Strategy,
    candles: Sequence[Candle],
    *,
    timeframe: Timeframe,
    portfolio: PortfolioView,
) -> Iterator[Signal]:
    """Evaluate every candle in ``candles`` (ascending) that has a full window behind it.

    Candles whose window is incomplete (warm-up, or a gap in history) are
    skipped, exactly as the live runner skips them. ``created_at`` is the
    candle's close time, so replay output is fully deterministic.
    """
    lookback = strategy.lookback
    for end in range(lookback, len(candles) + 1):
        window = candles[end - lookback : end]
        if window_problem(window, timeframe, lookback) is not None:
            continue
        signal = evaluate_candle(
            strategy,
            window=window,
            timeframe=timeframe,
            portfolio=portfolio,
            created_at=window[-1].close_time,
        )
        if signal is not None:
            yield signal
