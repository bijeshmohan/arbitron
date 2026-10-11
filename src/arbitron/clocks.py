"""Clock implementations for the ``arbitron.ports.clock.Clock`` port."""

from datetime import UTC, datetime, timedelta


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """A clock that only moves when told to; for tests, replay, and backtests."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None or start.utcoffset() != timedelta(0):
            msg = "ManualClock requires a timezone-aware UTC start time"
            raise ValueError(msg)
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        if delta < timedelta(0):
            msg = "ManualClock cannot move backwards"
            raise ValueError(msg)
        self._now += delta
        return self._now

    def set(self, moment: datetime) -> None:
        if moment < self._now:
            msg = "ManualClock cannot move backwards"
            raise ValueError(msg)
        self._now = moment
