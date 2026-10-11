from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    """Source of the current time. Must always return timezone-aware UTC.

    Everything that needs "now" takes a ``Clock`` so backtests and tests can
    control time and replays stay deterministic.
    """

    def now(self) -> datetime: ...
