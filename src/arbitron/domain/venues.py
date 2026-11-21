"""Supported venues and what each one needs from the adapters (spec: Hyperliquid design §3).

A venue is supported only if it has a profile here, plus adapter options and
contract tests. Anything else is refused rather than run with untested
assumptions. Profiles are data: adapter code reads them instead of branching
on a venue name.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from arbitron.domain.errors import DomainValidationError


class TickerSource(StrEnum):
    TICKER = "ticker"  # the venue's ticker endpoint
    ORDER_BOOK = "order_book"  # top of the order book (no usable native ticker)


class ClockSource(StrEnum):
    SERVER_TIME = "server_time"  # a server-time endpoint
    ORDER_BOOK = "order_book"  # the venue timestamp on an order-book snapshot


class ClientIdScheme(StrEnum):
    PLAIN = "plain"  # our ``arb`` + 29 hex ID as is
    HEX128 = "hex128"  # ``0x000`` + 29 hex (see ``ids.to_hex128_client_id``)


@dataclass(frozen=True, slots=True)
class VenueProfile:
    venue_id: str
    # ``VenueCapabilities`` field names the market-data adapter needs natively.
    market_data_capabilities: tuple[str, ...]
    ticker_source: TickerSource
    clock_source: ClockSource
    client_id_scheme: ClientIdScheme
    # Percent per fill, used by paper and backtest unless the config overrides it.
    default_taker_fee_pct: Decimal
    default_maker_fee_pct: Decimal
    # Whether this build has an authenticated (sandbox) trading adapter for it.
    supports_trading: bool


class UnknownVenueError(DomainValidationError):
    pass


VENUES: Mapping[str, VenueProfile] = MappingProxyType(
    {
        "binance": VenueProfile(
            venue_id="binance",
            market_data_capabilities=("fetch_ohlcv", "fetch_ticker", "fetch_time"),
            ticker_source=TickerSource.TICKER,
            clock_source=ClockSource.SERVER_TIME,
            client_id_scheme=ClientIdScheme.PLAIN,
            default_taker_fee_pct=Decimal("0.1"),
            default_maker_fee_pct=Decimal("0.1"),
            supports_trading=True,
        ),
        # Spot only. CCXT base-tier fees, checked 2026-11-15; trading arrives in phase 2.
        "hyperliquid": VenueProfile(
            venue_id="hyperliquid",
            market_data_capabilities=("fetch_ohlcv", "fetch_order_book"),
            ticker_source=TickerSource.ORDER_BOOK,
            clock_source=ClockSource.ORDER_BOOK,
            client_id_scheme=ClientIdScheme.HEX128,
            default_taker_fee_pct=Decimal("0.07"),
            default_maker_fee_pct=Decimal("0.04"),
            supports_trading=False,
        ),
    }
)


def venue_profile(venue_id: str) -> VenueProfile:
    profile = VENUES.get(venue_id)
    if profile is None:
        msg = f"exchange {venue_id!r} is not supported (supported: {sorted(VENUES)})"
        raise UnknownVenueError(msg)
    return profile
