import pytest

from arbitron.ports import account, execution, market_data, notifier, repositories
from arbitron.ports.errors import (
    AuthenticationFailure,
    ExchangeUnavailable,
    InsufficientFunds,
    InvalidOrder,
    MarketUnavailable,
    NetworkTransient,
    RateLimited,
    RequestTimeout,
    UnknownExchangeError,
    UnsupportedCapability,
    VenueError,
)


@pytest.mark.parametrize(
    "error", [NetworkTransient, RequestTimeout, RateLimited, ExchangeUnavailable]
)
def test_transient_errors_are_retryable_for_reads(error: type[VenueError]) -> None:
    assert error.retryable


@pytest.mark.parametrize(
    "error",
    [
        AuthenticationFailure,
        InsufficientFunds,
        InvalidOrder,
        MarketUnavailable,
        UnknownExchangeError,
        UnsupportedCapability,
    ],
)
def test_permanent_errors_are_not_retryable(error: type[VenueError]) -> None:
    assert not error.retryable
    assert issubclass(error, VenueError)


def test_port_modules_define_protocols() -> None:
    assert account.AccountPort
    assert execution.ExecutionPort
    assert market_data.MarketDataPort
    assert notifier.Notifier
    assert repositories.UnitOfWork
