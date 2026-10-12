"""Which signals may be acted on (operator decision 2026-10-12).

A signal is actionable only if it comes from the **latest closed candle** of
its market and we are still within **one timeframe interval** of that candle
closing. Signals persisted while catching up after downtime describe the
past and are never traded.
"""

from datetime import datetime

from arbitron.domain.market import Timeframe
from arbitron.domain.signals import Signal


def stale_reason(
    signal: Signal, *, latest_closed_open: datetime | None, now: datetime, timeframe: Timeframe
) -> str | None:
    """Why ``signal`` must not be acted on, or ``None`` if it may be."""
    if signal.timeframe != timeframe:
        return f"signal timeframe {signal.timeframe} is not {timeframe}"
    if latest_closed_open is None or signal.source_candle_time != latest_closed_open:
        return "a newer closed candle exists (signal is from an earlier candle)"
    candle_close = signal.source_candle_time + timeframe.duration
    if now - candle_close > timeframe.duration:
        return f"more than one {timeframe} interval has passed since the candle closed"
    return None
