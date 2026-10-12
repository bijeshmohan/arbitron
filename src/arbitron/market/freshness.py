"""Market-data quality gates (plan §9.2).

``assess`` is pure: given what is known about a symbol's data, it lists every
problem. Strategy evaluation and order creation must not proceed unless the
report is ``ok`` (Phases 4-5 enforce this; risk maps it to
``RiskReason.MARKET_DATA_STALE``).
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType

from arbitron.domain.enums import Severity
from arbitron.domain.market import Candle, Market, Timeframe
from arbitron.market.candles import missing_open_times, structural_problems
from arbitron.ports.clock import Clock
from arbitron.ports.market_data import MarketDataPort
from arbitron.ports.repositories import UnitOfWork


class DataIssueCode(StrEnum):
    NO_DATA = "no_data"
    STALE = "stale"
    INCOMPLETE_LOOKBACK = "incomplete_lookback"
    INVALID_SERIES = "invalid_series"
    MARKET_METADATA_MISSING = "market_metadata_missing"
    MARKET_METADATA_STALE = "market_metadata_stale"
    MARKET_INACTIVE = "market_inactive"
    CLOCK_UNVERIFIED = "clock_unverified"
    CLOCK_SKEW = "clock_skew"
    # Reported by the collector:
    GAP = "gap"
    CANDLE_REVISED = "candle_revised"
    BACKFILL_INCOMPLETE = "backfill_incomplete"
    VENUE_DATA_INVALID = "venue_data_invalid"


# Issues that are reported for operator attention but do not by themselves
# block trading. A revised candle is never overwritten (stored history stays
# as it was used), so blocking on it would block forever; it is alerted instead.
_NON_BLOCKING = frozenset({DataIssueCode.CANDLE_REVISED})


@dataclass(frozen=True, slots=True)
class DataIssue:
    code: DataIssueCode
    symbol: str
    detail: str

    @property
    def blocking(self) -> bool:
        return self.code not in _NON_BLOCKING

    @property
    def severity(self) -> Severity:
        return Severity.CRITICAL if self.blocking else Severity.WARNING


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    symbol: str
    as_of: datetime
    issues: tuple[DataIssue, ...]

    @property
    def ok(self) -> bool:
        return not any(issue.blocking for issue in self.issues)


@dataclass(frozen=True, slots=True)
class QualityThresholds:
    max_candle_age: timedelta
    metadata_max_age: timedelta
    max_clock_skew: timedelta


def assess(
    *,
    symbol: str,
    timeframe: Timeframe,
    now: datetime,
    window: Sequence[Candle],
    lookback: int,
    market: Market | None,
    markets_loaded_at: datetime | None,
    clock_offset: timedelta | None,
    thresholds: QualityThresholds,
) -> DataQualityReport:
    """Assess one symbol. ``window`` is the closed candles ending at the latest stored one."""
    issues: list[DataIssue] = []

    def add(code: DataIssueCode, detail: str) -> None:
        issues.append(DataIssue(code, symbol, detail))

    if market is None or markets_loaded_at is None:
        add(DataIssueCode.MARKET_METADATA_MISSING, "market metadata has not been loaded")
    else:
        if not market.active:
            add(DataIssueCode.MARKET_INACTIVE, f"{symbol} is not active on {market.exchange_id}")
        age = now - markets_loaded_at
        if age > thresholds.metadata_max_age:
            add(DataIssueCode.MARKET_METADATA_STALE, f"market metadata is {age} old")

    if clock_offset is None:
        add(DataIssueCode.CLOCK_UNVERIFIED, "clock offset against the venue is unknown")
    elif abs(clock_offset) > thresholds.max_clock_skew:
        add(DataIssueCode.CLOCK_SKEW, f"local clock differs from venue by {clock_offset}")

    if not window:
        add(DataIssueCode.NO_DATA, "no closed candles stored")
        return DataQualityReport(symbol, now, tuple(issues))

    for problem in structural_problems(window, timeframe):
        add(DataIssueCode.INVALID_SERIES, problem)

    latest = window[-1]
    age = now - latest.close_time
    if age > thresholds.max_candle_age:
        add(DataIssueCode.STALE, f"latest closed candle closed {age} ago")

    start = latest.open_time - (lookback - 1) * timeframe.duration
    in_window = [c for c in window if c.open_time >= start]
    missing = missing_open_times(in_window, timeframe, start=start, end=latest.close_time)
    if missing:
        add(
            DataIssueCode.INCOMPLETE_LOOKBACK,
            f"{len(missing)} of {lookback} lookback candles missing "
            f"(first missing {missing[0].isoformat()})",
        )
    return DataQualityReport(symbol, now, tuple(issues))


async def measure_clock_offset(market_data: MarketDataPort, clock: Clock) -> timedelta:
    """Venue time minus local time, using the request midpoint to cancel latency."""
    before = clock.now()
    venue_time = await market_data.fetch_time()
    after = clock.now()
    return venue_time - (before + (after - before) / 2)


class MarketDataMonitor:
    """Holds the latest market metadata and clock offset; evaluates the gate per symbol."""

    def __init__(
        self,
        *,
        exchange_id: str,
        timeframe: Timeframe,
        uow_factory: Callable[[], UnitOfWork],
        clock: Clock,
        thresholds: QualityThresholds,
    ) -> None:
        self._exchange_id = exchange_id
        self._timeframe = timeframe
        self._uow_factory = uow_factory
        self._clock = clock
        self._thresholds = thresholds
        self._markets: dict[str, Market] = {}
        self._markets_loaded_at: datetime | None = None
        self._clock_offset: timedelta | None = None

    async def refresh(self, market_data: MarketDataPort) -> None:
        """Reload market metadata and re-measure clock offset.

        On failure the previous values are kept, and they age into
        ``MARKET_METADATA_STALE`` if refresh keeps failing.
        """
        markets = await market_data.load_markets()
        self._markets = dict(markets)
        self._markets_loaded_at = self._clock.now()
        self._clock_offset = await measure_clock_offset(market_data, self._clock)

    @property
    def clock_offset(self) -> timedelta | None:
        return self._clock_offset

    @property
    def markets(self) -> Mapping[str, Market]:
        """Market metadata from the last successful refresh (empty before the first)."""
        return MappingProxyType(self._markets)

    async def evaluate(self, symbol: str, *, lookback: int) -> DataQualityReport:
        if lookback < 1:
            msg = "lookback must be >= 1"
            raise ValueError(msg)
        now = self._clock.now()
        duration = self._timeframe.duration
        async with self._uow_factory() as tx:
            latest = await tx.candles.latest_closed(self._exchange_id, symbol, self._timeframe)
            window: Sequence[Candle] = []
            if latest is not None:
                window = await tx.candles.closed_range(
                    self._exchange_id,
                    symbol,
                    self._timeframe,
                    start=latest.open_time - (lookback - 1) * duration,
                    end=latest.open_time + duration,
                )
        return assess(
            symbol=symbol,
            timeframe=self._timeframe,
            now=now,
            window=window,
            lookback=lookback,
            market=self._markets.get(symbol),
            markets_loaded_at=self._markets_loaded_at,
            clock_offset=self._clock_offset,
            thresholds=self._thresholds,
        )
