from datetime import datetime, timedelta

import pytest

from arbitron.clocks import ManualClock, SystemClock
from arbitron.ports.clock import Clock
from tests.factories import T0


def test_system_clock_is_utc() -> None:
    clock: Clock = SystemClock()

    assert clock.now().utcoffset() == timedelta(0)


def test_manual_clock_advances_only_forward() -> None:
    clock = ManualClock(T0)

    assert clock.advance(timedelta(minutes=5)) == T0 + timedelta(minutes=5)
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(timedelta(seconds=-1))
    with pytest.raises(ValueError, match="backwards"):
        clock.set(T0)


def test_manual_clock_requires_utc() -> None:
    with pytest.raises(ValueError, match="UTC"):
        ManualClock(datetime(2026, 1, 1))  # noqa: DTZ001
