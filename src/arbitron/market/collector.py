"""Closed-candle ingestion (plan §9.1).

Each ``sync`` fetches forward from the latest stored closed candle (re-fetching
that candle to detect venue revisions), persists only closed candles, and
records every data-quality problem as a system event. Structurally invalid
batches (duplicates, out-of-order, misaligned) or invalid venue data are
never persisted.
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from arbitron.domain.events import SystemEvent
from arbitron.domain.market import Candle, Timeframe
from arbitron.market.candles import align_down, missing_open_times, structural_problems
from arbitron.market.freshness import DataIssue, DataIssueCode
from arbitron.ports.clock import Clock
from arbitron.ports.errors import InvalidVenueData
from arbitron.ports.market_data import MarketDataPort
from arbitron.ports.repositories import UnitOfWork

COMPONENT = "market.collector"


@dataclass(frozen=True, slots=True)
class SyncResult:
    symbol: str
    fetched: int
    written: int
    latest_closed: datetime | None
    issues: tuple[DataIssue, ...]


class CandleCollector:
    def __init__(
        self,
        *,
        market_data: MarketDataPort,
        uow_factory: Callable[[], UnitOfWork],
        clock: Clock,
        exchange_id: str,
        timeframe: Timeframe,
        backfill_candles: int,
        page_limit: int = 1000,
        max_pages: int = 20,
    ) -> None:
        self._market_data = market_data
        self._uow_factory = uow_factory
        self._clock = clock
        self._exchange_id = exchange_id
        self._timeframe = timeframe
        self._backfill_candles = backfill_candles
        self._page_limit = page_limit
        self._max_pages = max_pages

    async def sync(self, symbol: str) -> SyncResult:
        correlation_id = f"sync-{uuid.uuid4()}"
        async with self._uow_factory() as tx:
            stored_latest = await tx.candles.latest_closed(
                self._exchange_id, symbol, self._timeframe
            )

        now = self._clock.now()
        if stored_latest is None:
            since = (
                align_down(now, self._timeframe) - self._backfill_candles * self._timeframe.duration
            )
        else:
            since = stored_latest.open_time  # overlap one candle to detect revisions

        issues: list[DataIssue] = []
        try:
            fetched, complete = await self._fetch_closed(symbol, since)
        except InvalidVenueData as exc:
            issues.append(DataIssue(DataIssueCode.VENUE_DATA_INVALID, symbol, str(exc)))
            await self._record(issues, correlation_id)
            return SyncResult(symbol, 0, 0, _open_time(stored_latest), tuple(issues))

        problems = structural_problems(fetched, self._timeframe)
        if problems:
            issues.extend(DataIssue(DataIssueCode.INVALID_SERIES, symbol, p) for p in problems)
            await self._record(issues, correlation_id)
            return SyncResult(symbol, len(fetched), 0, _open_time(stored_latest), tuple(issues))

        new: Sequence[Candle] = fetched
        if (
            stored_latest is not None
            and fetched
            and fetched[0].open_time == stored_latest.open_time
        ):
            if not _same_values(fetched[0], stored_latest):
                issues.append(
                    DataIssue(
                        DataIssueCode.CANDLE_REVISED,
                        symbol,
                        f"venue revised closed candle at {stored_latest.open_time.isoformat()}; "
                        "stored values kept",
                    )
                )
            new = fetched[1:]

        if not complete:
            issues.append(
                DataIssue(
                    DataIssueCode.BACKFILL_INCOMPLETE,
                    symbol,
                    f"stopped after {self._max_pages} pages; more history remains",
                )
            )

        if new:
            gap_start = stored_latest.close_time if stored_latest is not None else new[0].open_time
            missing = missing_open_times(
                new, self._timeframe, start=gap_start, end=new[-1].close_time
            )
            if missing:
                issues.append(
                    DataIssue(
                        DataIssueCode.GAP,
                        symbol,
                        f"venue returned no candle for {len(missing)} interval(s), "
                        f"first {missing[0].isoformat()}",
                    )
                )

        async with self._uow_factory() as tx:
            written = await tx.candles.upsert(new) if new else 0
            for event in self._events(issues, correlation_id):
                await tx.system_events.append(event)
            await tx.commit()

        latest = new[-1].open_time if new else _open_time(stored_latest)
        return SyncResult(symbol, len(fetched), written, latest, tuple(issues))

    async def _fetch_closed(self, symbol: str, since: datetime) -> tuple[list[Candle], bool]:
        """Closed candles from ``since`` onward, paging until caught up or out of pages."""
        closed: list[Candle] = []
        for _ in range(self._max_pages):
            page = await self._market_data.fetch_ohlcv(
                symbol, self._timeframe, since=since, limit=self._page_limit
            )
            closed.extend(c for c in page if c.is_closed)
            if len(page) < self._page_limit or not page:
                return closed, True
            next_since = page[-1].open_time + self._timeframe.duration
            if next_since <= since:
                msg = f"{symbol}: venue pagination did not advance past {since.isoformat()}"
                raise InvalidVenueData(msg)
            since = next_since
        return closed, False

    def _events(self, issues: Sequence[DataIssue], correlation_id: str) -> list[SystemEvent]:
        now = self._clock.now()
        return [
            SystemEvent(
                event_type=f"market_data.{issue.code.value}",
                severity=issue.severity,
                component=COMPONENT,
                occurred_at=now,
                correlation_id=correlation_id,
                payload={
                    "exchange_id": self._exchange_id,
                    "symbol": issue.symbol,
                    "timeframe": self._timeframe.value,
                    "detail": issue.detail,
                },
            )
            for issue in issues
        ]

    async def _record(self, issues: Sequence[DataIssue], correlation_id: str) -> None:
        async with self._uow_factory() as tx:
            for event in self._events(issues, correlation_id):
                await tx.system_events.append(event)
            await tx.commit()


def _same_values(a: Candle, b: Candle) -> bool:
    return (a.open, a.high, a.low, a.close, a.volume) == (b.open, b.high, b.low, b.close, b.volume)


def _open_time(candle: Candle | None) -> datetime | None:
    return candle.open_time if candle is not None else None
