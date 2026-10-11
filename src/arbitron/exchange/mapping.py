"""CCXT payload -> domain conversions.

CCXT parses venue numbers into Python floats. A float that came from a short
decimal string (as all venue prices and quantities do) round-trips through
``repr`` to that same shortest decimal string, so ``Decimal(repr(value))``
recovers the venue's exact value without binary noise. Strings and integers
are converted directly. Anything else, or any non-finite value, is invalid.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import cast

from arbitron.domain.enums import MarketType
from arbitron.domain.errors import DomainError
from arbitron.domain.market import Candle, Market, Ticker, Timeframe
from arbitron.ports.errors import InvalidVenueData

# CCXT precision modes (ccxt.base.decimal_to_precision).
DECIMAL_PLACES = 2
SIGNIFICANT_DIGITS = 3
TICK_SIZE = 4


def decimal_from_ccxt(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        msg = f"{field}: expected a number, got bool"
        raise InvalidVenueData(msg)
    try:
        if isinstance(value, float):
            result = Decimal(repr(value))
        elif isinstance(value, int | str):
            result = Decimal(value)
        else:
            msg = f"{field}: expected a number, got {type(value).__name__}"
            raise InvalidVenueData(msg)
    except InvalidOperation as exc:
        msg = f"{field}: invalid number {value!r}"
        raise InvalidVenueData(msg) from exc
    if not result.is_finite():
        msg = f"{field}: non-finite value {value!r}"
        raise InvalidVenueData(msg)
    return result


def optional_decimal(value: object, field: str) -> Decimal | None:
    return None if value is None else decimal_from_ccxt(value, field)


def datetime_from_ms(value: object, field: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{field}: expected epoch milliseconds, got {value!r}"
        raise InvalidVenueData(msg)
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=int(value))


def datetime_to_ms(value: datetime) -> int:
    return int((value - datetime(1970, 1, 1, tzinfo=UTC)) / timedelta(milliseconds=1))


def _step(value: object, precision_mode: int, field: str) -> Decimal:
    """Convert a CCXT precision value to a step (tick) size."""
    number = decimal_from_ccxt(value, field)
    if precision_mode == TICK_SIZE:
        return number
    if precision_mode == DECIMAL_PLACES:
        return Decimal(1).scaleb(-int(number))
    msg = f"{field}: unsupported CCXT precision mode {precision_mode}"
    raise InvalidVenueData(msg)


def _child(value: object, key: str) -> object:
    if isinstance(value, dict):
        return cast("dict[str, object]", value).get(key)
    return None


def _get(mapping: Mapping[str, object], *path: str) -> object:
    """Nested lookup into CCXT's plain-dict payloads; ``None`` if any level is missing."""
    current: object = mapping
    for key in path:
        current = _child(current, key)
    return current


def market_from_ccxt(raw: Mapping[str, object], *, exchange_id: str, precision_mode: int) -> Market:
    symbol = raw.get("symbol")
    try:
        if raw.get("spot") is not True:
            msg = f"{symbol}: not a spot market"
            raise InvalidVenueData(msg)
        min_amount = optional_decimal(_get(raw, "limits", "amount", "min"), "limits.amount.min")
        min_cost = optional_decimal(_get(raw, "limits", "cost", "min"), "limits.cost.min")
        return Market(
            exchange_id=exchange_id,
            symbol=str(symbol),
            base=str(raw.get("base")),
            quote=str(raw.get("quote")),
            market_type=MarketType.SPOT,
            amount_step=_step(_get(raw, "precision", "amount"), precision_mode, "precision.amount"),
            price_step=_step(_get(raw, "precision", "price"), precision_mode, "precision.price"),
            min_amount=min_amount if min_amount is not None else Decimal(0),
            min_cost=min_cost if min_cost is not None else Decimal(0),
            active=raw.get("active") is True,
        )
    except DomainError as exc:
        msg = f"{symbol}: invalid market metadata: {exc}"
        raise InvalidVenueData(msg) from exc


def candles_from_ohlcv(
    rows: Sequence[Sequence[object]],
    *,
    exchange_id: str,
    symbol: str,
    timeframe: Timeframe,
    now: datetime,
    close_grace: timedelta,
) -> list[Candle]:
    """Convert CCXT OHLCV rows. A candle is closed once ``close_time + close_grace <= now``."""
    candles: list[Candle] = []
    for index, row in enumerate(rows):
        if len(row) < 6:  # OHLCV arity
            msg = f"{symbol} ohlcv[{index}]: expected 6 fields, got {len(row)}"
            raise InvalidVenueData(msg)
        open_time = datetime_from_ms(row[0], f"ohlcv[{index}].time")
        try:
            candle = Candle(
                exchange_id=exchange_id,
                symbol=symbol,
                timeframe=timeframe,
                open_time=open_time,
                open=decimal_from_ccxt(row[1], f"ohlcv[{index}].open"),
                high=decimal_from_ccxt(row[2], f"ohlcv[{index}].high"),
                low=decimal_from_ccxt(row[3], f"ohlcv[{index}].low"),
                close=decimal_from_ccxt(row[4], f"ohlcv[{index}].close"),
                volume=decimal_from_ccxt(row[5], f"ohlcv[{index}].volume"),
                is_closed=open_time + timeframe.duration + close_grace <= now,
                ingested_at=now,
            )
        except DomainError as exc:
            msg = f"{symbol} ohlcv[{index}] at {open_time.isoformat()}: {exc}"
            raise InvalidVenueData(msg) from exc
        candles.append(candle)
    return candles


def ticker_from_ccxt(raw: Mapping[str, object], *, exchange_id: str, symbol: str) -> Ticker:
    try:
        return Ticker(
            exchange_id=exchange_id,
            symbol=symbol,
            bid=optional_decimal(raw.get("bid"), "bid"),
            ask=optional_decimal(raw.get("ask"), "ask"),
            last=optional_decimal(raw.get("last"), "last"),
            timestamp=datetime_from_ms(raw.get("timestamp"), "timestamp"),
        )
    except DomainError as exc:
        msg = f"{symbol}: invalid ticker: {exc}"
        raise InvalidVenueData(msg) from exc
