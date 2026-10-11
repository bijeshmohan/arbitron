"""Candle-series utilities: alignment, structural validation, and gap detection."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from arbitron.domain.market import Candle, Timeframe

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# Binance (and most venues) start weekly candles on Monday 00:00 UTC;
# 1970-01-01 was a Thursday.
_FIRST_MONDAY = datetime(1970, 1, 5, tzinfo=UTC)
_DAY = timedelta(days=1)
_WEEK = timedelta(weeks=1)


def _anchor(timeframe: Timeframe) -> datetime | None:
    """Reference instant candles align to, or ``None`` if alignment is venue-defined."""
    duration = timeframe.duration
    if duration == _WEEK:
        return _FIRST_MONDAY
    if duration <= _DAY and _DAY % duration == timedelta(0):
        return _EPOCH
    return None  # e.g. 3d: alignment varies by venue, so it is not checked


def align_down(moment: datetime, timeframe: Timeframe) -> datetime:
    """Open time of the candle containing ``moment``.

    Timeframes with venue-defined alignment fall back to epoch alignment.
    """
    anchor = _anchor(timeframe) or _EPOCH
    duration = timeframe.duration
    return anchor + ((moment - anchor) // duration) * duration


def is_aligned(open_time: datetime, timeframe: Timeframe) -> bool:
    anchor = _anchor(timeframe)
    return anchor is None or (open_time - anchor) % timeframe.duration == timedelta(0)


def latest_expected_closed_open(now: datetime, timeframe: Timeframe) -> datetime:
    """Open time of the most recent candle that should have closed by ``now``."""
    return align_down(now, timeframe) - timeframe.duration


def structural_problems(candles: Sequence[Candle], timeframe: Timeframe) -> list[str]:
    """Duplicates, out-of-order, misaligned, or mixed-identity candles in a fetched batch."""
    problems: list[str] = []
    for index, candle in enumerate(candles):
        if candle.timeframe != timeframe:
            problems.append(
                f"candle {index} has timeframe {candle.timeframe}, expected {timeframe}"
            )
        if not is_aligned(candle.open_time, timeframe):
            problems.append(f"candle at {candle.open_time.isoformat()} is misaligned")
        if index == 0:
            continue
        previous = candles[index - 1]
        if (candle.exchange_id, candle.symbol) != (previous.exchange_id, previous.symbol):
            problems.append(f"candle {index} belongs to a different market")
        if candle.open_time == previous.open_time:
            problems.append(f"duplicate candle at {candle.open_time.isoformat()}")
        elif candle.open_time < previous.open_time:
            problems.append(f"out-of-order candle at {candle.open_time.isoformat()}")
    return problems


def missing_open_times(
    candles: Sequence[Candle], timeframe: Timeframe, *, start: datetime, end: datetime
) -> list[datetime]:
    """Open times in ``[start, end)`` with no candle. ``candles`` must be ascending."""
    present = {candle.open_time for candle in candles}
    missing: list[datetime] = []
    current = start
    while current < end:
        if current not in present:
            missing.append(current)
        current += timeframe.duration
    return missing
