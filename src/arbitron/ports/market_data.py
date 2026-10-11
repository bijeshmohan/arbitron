from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from arbitron.domain.market import Candle, Market, Ticker, Timeframe


class MarketDataPort(Protocol):
    """Read-only public market data. Requires no trading credentials.

    Raises ``arbitron.ports.errors.VenueError`` subclasses on failure.
    """

    async def load_markets(self) -> Mapping[str, Market]:
        """(Re)load metadata for the configured symbol allowlist, keyed by unified symbol.

        Raises ``MarketUnavailable`` if an allowlisted symbol is missing or not
        spot. Inactive markets are returned with ``active=False``; the data
        quality gate decides what that blocks.
        """
        ...

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Sequence[Candle]:
        """Candles in ascending open-time order, including the current unfinished
        candle if the venue returns it (marked ``is_closed=False``)."""
        ...

    async def fetch_ticker(self, symbol: str) -> Ticker: ...

    async def fetch_time(self) -> datetime:
        """The venue's current time, for clock-skew checks."""
        ...

    async def close(self) -> None: ...
