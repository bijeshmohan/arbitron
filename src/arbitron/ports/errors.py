"""Venue error taxonomy (plan §8.1).

Adapters translate library-specific exceptions into these categories so the
application can decide retry and safety behavior without importing CCXT.

``retryable`` describes *reads* only. Order submission is never retried
blindly: any error raised after the request may have reached the venue
(``RequestTimeout``, ``NetworkTransient`` after send, ``ExchangeUnavailable``,
``UnknownExchangeError``) leaves the outcome unknown, and the order manager
must reconcile before any further submission.
"""

from typing import ClassVar


class VenueError(Exception):
    retryable: ClassVar[bool] = False


class NetworkTransient(VenueError):
    retryable = True


class RequestTimeout(VenueError):
    retryable = True


class RateLimited(VenueError):
    retryable = True


class ExchangeUnavailable(VenueError):
    retryable = True


class AuthenticationFailure(VenueError):
    pass


class InsufficientFunds(VenueError):
    pass


class InvalidOrder(VenueError):
    pass


class MarketUnavailable(VenueError):
    pass


class UnknownExchangeError(VenueError):
    pass


class UnsupportedCapability(VenueError):
    """The venue (or its sandbox) does not support a required operation."""


class InvalidVenueData(VenueError):
    """The venue returned data that fails validation (malformed or violating invariants).

    Never persisted; callers record a data-quality event and fail closed.
    """
