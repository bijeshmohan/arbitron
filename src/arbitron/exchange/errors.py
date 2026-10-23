"""Translate CCXT exceptions into the venue error taxonomy (plan §8.1).

Order matters: CCXT's hierarchy nests specific errors under general ones, so
the most specific match must be checked first.
"""

import asyncio
from collections.abc import Generator
from contextlib import contextmanager

import ccxt

from arbitron.ports import errors as venue

_TRANSLATIONS: tuple[tuple[type[BaseException], type[venue.VenueError]], ...] = (
    # Network layer (retryable for reads)
    (ccxt.RequestTimeout, venue.RequestTimeout),
    (ccxt.RateLimitExceeded, venue.RateLimited),
    (ccxt.DDoSProtection, venue.RateLimited),  # includes Binance HTTP 418 IP bans
    (ccxt.OnMaintenance, venue.ExchangeUnavailable),
    (ccxt.ExchangeNotAvailable, venue.ExchangeUnavailable),
    (ccxt.NetworkError, venue.NetworkTransient),
    # Order-specific. A duplicate client order ID means an order with this ID
    # may already exist: the outcome is unknown and must be reconciled.
    (ccxt.DuplicateOrderId, venue.UnknownExchangeError),
    (ccxt.InvalidOrder, venue.InvalidOrder),
    (ccxt.InsufficientFunds, venue.InsufficientFunds),
    # Access. Regional restriction is permanent for our purposes, like bad keys.
    (ccxt.RestrictedLocation, venue.AuthenticationFailure),
    (ccxt.AuthenticationError, venue.AuthenticationFailure),
    (ccxt.BadSymbol, venue.MarketUnavailable),
    # The venue refused a request it judged malformed (HTTP 400), e.g. Binance
    # -1013 "Filter failure: NOTIONAL" on an order: definitively not placed.
    # Confirmed against the Binance spot testnet (docs/sandbox-qualification.md).
    (ccxt.BadRequest, venue.InvalidOrder),
    (ccxt.MarketClosed, venue.MarketUnavailable),
    (ccxt.NotSupported, venue.UnsupportedCapability),
    # Everything else, including malformed responses, is unknown and not retried.
    (ccxt.BaseError, venue.UnknownExchangeError),
    # Our own outer timeout guard around a CCXT call.
    (TimeoutError, venue.RequestTimeout),
    (asyncio.TimeoutError, venue.RequestTimeout),
)


def translate(exc: BaseException) -> venue.VenueError | None:
    """The venue error for ``exc``, or ``None`` if it is not a venue failure."""
    if isinstance(exc, venue.VenueError):
        return exc
    for source, target in _TRANSLATIONS:
        if isinstance(exc, source):
            # Message only: CCXT messages can include request URLs but never
            # credentials; headers and bodies are not part of str(exc).
            return target(f"{type(exc).__name__}: {exc}")
    return None


@contextmanager
def venue_errors() -> Generator[None]:
    """Re-raise CCXT (and timeout) exceptions as venue errors, chained to the original."""
    try:
        yield
    except Exception as exc:
        translated = translate(exc)
        if translated is None or translated is exc:
            raise
        raise translated from exc
