"""Historical candle backfill for backtests (operator decision 2026-10-13).

Pages the venue's closed candles for ``[start, end)`` into the same
``candles`` table the live collector uses, with the same validation:
structurally invalid pages are refused, closed candles already stored are
never modified, and every venue gap is reported. Market trading rules are
stored alongside, so backtests can run offline from the database.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from arbitron.domain.market import Candle, Timeframe
from arbitron.market.candles import align_down, missing_open_times, structural_problems
from arbitron.ports.clock import Clock
from arbitron.ports.errors import InvalidVenueData
from arbitron.ports.market_data import MarketDataPort
from arbitron.ports.repositories import UnitOfWork

PAGE_LIMIT = 1000


@dataclass
class BackfillResult:
    symbol: str
    start: datetime
    end: datetime
    fetched: int = 0
    written: int = 0
    pages: int = 0
    gaps: list[tuple[datetime, datetime]] = field(default_factory=list[tuple[datetime, datetime]])


class Backfiller:
    def __init__(
        self,
        *,
        market_data: MarketDataPort,
        uow_factory: Callable[[], UnitOfWork],
        clock: Clock,
        exchange_id: str,
        timeframe: Timeframe,
        page_limit: int = PAGE_LIMIT,
    ) -> None:
        self._market_data = market_data
        self._uow_factory = uow_factory
        self._clock = clock
        self._exchange_id = exchange_id
        self._timeframe = timeframe
        self._page_limit = page_limit

    async def store_markets(self, symbols: tuple[str, ...]) -> int:
        markets = await self._market_data.load_markets()
        async with self._uow_factory() as tx:
            for symbol in symbols:
                await tx.markets.save(markets[symbol], observed_at=self._clock.now())
            await tx.commit()
        return len(symbols)

    async def run(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        progress: Callable[[BackfillResult], None] | None = None,
    ) -> BackfillResult:
        duration = self._timeframe.duration
        start = align_down(start, self._timeframe)
        result = BackfillResult(symbol, start, end)
        since = start
        stored: list[Candle] = []
        while since < end:
            page = await self._market_data.fetch_ohlcv(
                symbol, self._timeframe, since=since, limit=self._page_limit
            )
            result.pages += 1
            closed = [c for c in page if c.is_closed and since <= c.open_time < end]
            problems = structural_problems(closed, self._timeframe)
            if problems:
                msg = f"{symbol}: invalid page from {since.isoformat()}: {problems[0]}"
                raise InvalidVenueData(msg)
            if closed:
                async with self._uow_factory() as tx:
                    result.written += await tx.candles.upsert(closed)
                    await tx.commit()
                result.fetched += len(closed)
                stored.extend(closed)
            if not page:
                break
            next_since = page[-1].open_time + duration
            if next_since <= since:
                msg = f"{symbol}: venue pagination did not advance past {since.isoformat()}"
                raise InvalidVenueData(msg)
            since = next_since
            if progress is not None:
                progress(result)
            if len(page) < self._page_limit:
                break
        last = stored[-1].close_time if stored else start
        result.gaps = _ranges(
            missing_open_times(stored, self._timeframe, start=start, end=min(end, last)), duration
        )
        return result


def _ranges(missing: list[datetime], step: timedelta) -> list[tuple[datetime, datetime]]:
    """Collapse consecutive missing open times into [first, last] ranges."""
    ranges: list[tuple[datetime, datetime]] = []
    for moment in missing:
        if ranges and moment - ranges[-1][1] == step:
            ranges[-1] = (ranges[-1][0], moment)
        else:
            ranges.append((moment, moment))
    return ranges
