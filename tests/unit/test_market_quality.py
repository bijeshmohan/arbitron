from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from arbitron.domain.enums import MarketType, Severity
from arbitron.domain.market import Candle, Market, Timeframe
from arbitron.market.candles import (
    align_down,
    is_aligned,
    latest_expected_closed_open,
    missing_open_times,
    structural_problems,
)
from arbitron.market.freshness import (
    DataIssue,
    DataIssueCode,
    QualityThresholds,
    assess,
)
from tests.factories import FIVE_MIN, T0, make_candle

C = DataIssueCode


def series(count: int, start: datetime = T0, timeframe: Timeframe = FIVE_MIN) -> list[Candle]:
    return [
        make_candle(
            timeframe=timeframe,
            open_time=start + i * timeframe.duration,
            ingested_at=start + (i + 1) * timeframe.duration,
        )
        for i in range(count)
    ]


class TestAlignment:
    @pytest.mark.parametrize(
        ("moment", "timeframe", "expected"),
        [
            (
                datetime(2026, 1, 1, 12, 7, 30, tzinfo=UTC),
                "5m",
                datetime(2026, 1, 1, 12, 5, tzinfo=UTC),
            ),
            (
                datetime(2026, 1, 1, 13, 59, tzinfo=UTC),
                "4h",
                datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            ),
            (datetime(2026, 1, 1, 23, 0, tzinfo=UTC), "1d", datetime(2026, 1, 1, tzinfo=UTC)),
            # 2026-01-01 is a Thursday; weekly candles open on Monday 2025-12-29.
            (datetime(2026, 1, 1, 12, 0, tzinfo=UTC), "1w", datetime(2025, 12, 29, tzinfo=UTC)),
        ],
    )
    def test_align_down(self, moment: datetime, timeframe: str, expected: datetime) -> None:
        assert align_down(moment, Timeframe(timeframe)) == expected
        assert is_aligned(expected, Timeframe(timeframe))

    def test_misaligned_detected_but_3d_unchecked(self) -> None:
        assert not is_aligned(datetime(2026, 1, 1, 12, 3, tzinfo=UTC), FIVE_MIN)
        assert is_aligned(datetime(2026, 1, 1, 12, 3, tzinfo=UTC), Timeframe("3d"))

    def test_latest_expected_closed(self) -> None:
        now = datetime(2026, 1, 1, 12, 7, tzinfo=UTC)

        assert latest_expected_closed_open(now, FIVE_MIN) == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class TestSeries:
    def test_clean_series_has_no_problems(self) -> None:
        assert structural_problems(series(5), FIVE_MIN) == []

    def test_duplicate_out_of_order_misaligned_and_mixed(self) -> None:
        candles = series(3)
        bad = [
            candles[0],
            candles[0],  # duplicate
            candles[2],
            candles[1],  # out of order
            make_candle(open_time=T0 + timedelta(minutes=13), ingested_at=T0 + timedelta(hours=1)),
            make_candle(
                symbol="ETH/USDT",
                open_time=T0 + timedelta(minutes=20),
                ingested_at=T0 + timedelta(hours=1),
            ),
        ]
        problems = " | ".join(structural_problems(bad, FIVE_MIN))

        assert "duplicate" in problems
        assert "out-of-order" in problems
        assert "misaligned" in problems
        assert "different market" in problems

    def test_wrong_timeframe(self) -> None:
        problems = structural_problems(series(2, timeframe=Timeframe("1h")), FIVE_MIN)

        assert any("timeframe" in p for p in problems)

    def test_missing_open_times(self) -> None:
        candles = series(6)
        del candles[2]
        del candles[3]

        assert missing_open_times(candles, FIVE_MIN, start=T0, end=T0 + timedelta(minutes=30)) == [
            T0 + timedelta(minutes=10),
            T0 + timedelta(minutes=20),
        ]


MARKET = Market(
    "binance",
    "BTC/USDT",
    "BTC",
    "USDT",
    MarketType.SPOT,
    Decimal("0.00001"),
    Decimal("0.01"),
    Decimal("0.00001"),
    Decimal(5),
    active=True,
)
THRESHOLDS = QualityThresholds(
    max_candle_age=timedelta(minutes=10),
    metadata_max_age=timedelta(hours=1),
    max_clock_skew=timedelta(seconds=1),
)


def run_assess(**overrides: Any) -> list[DataIssueCode]:
    window = series(20)
    fields: dict[str, Any] = {
        "symbol": "BTC/USDT",
        "timeframe": FIVE_MIN,
        "now": window[-1].close_time + timedelta(seconds=30),
        "window": window,
        "lookback": 20,
        "market": MARKET,
        "markets_loaded_at": window[-1].close_time,
        "clock_offset": timedelta(milliseconds=20),
        "thresholds": THRESHOLDS,
    }
    fields.update(overrides)
    report = assess(**fields)
    codes = [issue.code for issue in report.issues]
    assert report.ok is (not [c for c in codes if c is not C.CANDLE_REVISED])
    return codes


class TestAssess:
    def test_healthy_data_passes(self) -> None:
        assert run_assess() == []

    def test_no_data(self) -> None:
        assert run_assess(window=[]) == [C.NO_DATA]

    def test_stale(self) -> None:
        now = series(20)[-1].close_time + timedelta(minutes=10, seconds=1)

        assert C.STALE in run_assess(now=now, markets_loaded_at=now)

    def test_incomplete_lookback(self) -> None:
        window = series(20)
        del window[5]

        assert run_assess(window=window) == [C.INCOMPLETE_LOOKBACK]

    def test_too_short_history(self) -> None:
        assert run_assess(window=series(20)[-10:]) == [C.INCOMPLETE_LOOKBACK]

    def test_lookback_smaller_than_window_ignores_older_gaps(self) -> None:
        window = series(20)
        del window[2]

        assert run_assess(window=window, lookback=10) == []

    def test_invalid_series(self) -> None:
        window = series(20)
        window.insert(5, window[5])

        assert C.INVALID_SERIES in run_assess(window=window)

    def test_market_metadata_problems(self) -> None:
        assert C.MARKET_METADATA_MISSING in run_assess(market=None)
        assert C.MARKET_METADATA_MISSING in run_assess(markets_loaded_at=None)
        assert C.MARKET_INACTIVE in run_assess(market=replace(MARKET, active=False))
        assert C.MARKET_METADATA_STALE in run_assess(markets_loaded_at=T0 - timedelta(days=1))

    def test_clock_problems(self) -> None:
        assert run_assess(clock_offset=None) == [C.CLOCK_UNVERIFIED]
        assert run_assess(clock_offset=timedelta(seconds=-2)) == [C.CLOCK_SKEW]

    def test_revised_candle_is_warning_only(self) -> None:
        issue = DataIssue(C.CANDLE_REVISED, "BTC/USDT", "x")

        assert not issue.blocking
        assert issue.severity is Severity.WARNING
        assert DataIssue(C.GAP, "BTC/USDT", "x").severity is Severity.CRITICAL
