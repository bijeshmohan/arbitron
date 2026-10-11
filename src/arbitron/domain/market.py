import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from arbitron.domain._checks import (
    require_non_negative,
    require_positive,
    require_text,
    require_utc,
)
from arbitron.domain.enums import MarketType
from arbitron.domain.errors import DomainValidationError

_TIMEFRAME_RE = re.compile(r"^([1-9][0-9]*)([mhdw])$")
_TIMEFRAME_UNITS = {
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
    "w": timedelta(weeks=1),
}


def _parse_timeframe(value: str) -> timedelta:
    match = _TIMEFRAME_RE.match(value)
    if match is None:
        msg = f"invalid timeframe {value!r}; expected e.g. '1m', '5m', '1h', '1d'"
        raise DomainValidationError(msg)
    return int(match.group(1)) * _TIMEFRAME_UNITS[match.group(2)]


@dataclass(frozen=True, slots=True)
class Timeframe:
    """A fixed-length candle interval such as ``5m`` or ``1h``.

    Calendar-month timeframes are deliberately unsupported: their length varies.
    """

    value: str

    def __post_init__(self) -> None:
        _parse_timeframe(self.value)

    @property
    def duration(self) -> timedelta:
        return _parse_timeframe(self.value)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Market:
    exchange_id: str
    symbol: str  # unified symbol, e.g. "BTC/USDT"
    base: str
    quote: str
    market_type: MarketType
    amount_step: Decimal
    price_step: Decimal
    min_amount: Decimal
    min_cost: Decimal
    active: bool

    def __post_init__(self) -> None:
        require_text("exchange_id", self.exchange_id)
        require_text("base", self.base)
        require_text("quote", self.quote)
        if self.symbol != f"{self.base}/{self.quote}":
            msg = f"symbol {self.symbol!r} does not match base/quote {self.base}/{self.quote}"
            raise DomainValidationError(msg)
        require_positive("amount_step", self.amount_step)
        require_positive("price_step", self.price_step)
        require_non_negative("min_amount", self.min_amount)
        require_non_negative("min_cost", self.min_cost)


@dataclass(frozen=True, slots=True)
class Candle:
    exchange_id: str
    symbol: str
    timeframe: Timeframe
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    is_closed: bool
    ingested_at: datetime

    def __post_init__(self) -> None:
        require_text("exchange_id", self.exchange_id)
        require_text("symbol", self.symbol)
        require_utc("open_time", self.open_time)
        require_utc("ingested_at", self.ingested_at)
        for name in ("open", "high", "low", "close"):
            require_positive(name, getattr(self, name))
        require_non_negative("volume", self.volume)
        if not (self.low <= min(self.open, self.close) and max(self.open, self.close) <= self.high):
            msg = (
                f"OHLC invariant violated: low={self.low} open={self.open} "
                f"close={self.close} high={self.high}"
            )
            raise DomainValidationError(msg)
        if self.is_closed and self.ingested_at < self.close_time:
            msg = "a candle cannot be ingested as closed before its close time"
            raise DomainValidationError(msg)

    @property
    def close_time(self) -> datetime:
        return self.open_time + self.timeframe.duration

    @property
    def identity(self) -> tuple[str, str, str, datetime]:
        return (self.exchange_id, self.symbol, self.timeframe.value, self.open_time)


@dataclass(frozen=True, slots=True)
class Ticker:
    exchange_id: str
    symbol: str
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    timestamp: datetime

    def __post_init__(self) -> None:
        require_text("exchange_id", self.exchange_id)
        require_text("symbol", self.symbol)
        require_utc("timestamp", self.timestamp)
        for name in ("bid", "ask", "last"):
            value = getattr(self, name)
            if value is not None:
                require_positive(name, value)
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            msg = f"crossed ticker: bid {self.bid} > ask {self.ask}"
            raise DomainValidationError(msg)
