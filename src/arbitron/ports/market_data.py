from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from arbitron.domain.market import Candle, Market, Ticker, Timeframe


class MarketDataPort(Protocol):
    """Read-only public market data. Requires no trading credentials.

    Raises ``arbitron.ports.errors.VenueError`` subclasses on failure.
    """

    async def load_markets(self) -> Mapping[str, Market]:
        """Markets keyed by unified symbol."""
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

    async def close(self) -> None: ...
