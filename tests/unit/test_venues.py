"""Venue profiles and the 128-bit client order ID codec (Hyperliquid ``cloid``)."""

from decimal import Decimal
from uuid import uuid4

import pytest

from arbitron.domain.errors import DomainValidationError
from arbitron.domain.ids import client_order_id_for, from_hex128_client_id, to_hex128_client_id
from arbitron.domain.venues import (
    VENUES,
    ClientIdScheme,
    ClockSource,
    TickerSource,
    UnknownVenueError,
    venue_profile,
)


def test_hex128_round_trip() -> None:
    cid = client_order_id_for(uuid4())
    venue = to_hex128_client_id(cid)
    assert venue == "0x000" + cid[3:]
    assert len(venue) == 34
    assert from_hex128_client_id(venue) == cid


@pytest.mark.parametrize(
    "value",
    [
        "0x" + "1" * 32,  # not our prefix: a foreign order
        "0x000" + "a" * 28,  # too short
        "0x000" + "a" * 30,  # too long
        "0x000" + "A" * 29,  # upper case is not canonical
        "000" + "a" * 31,  # no 0x
        "",
    ],
)
def test_foreign_or_malformed_venue_ids_decode_to_none(value: str) -> None:
    assert from_hex128_client_id(value) is None


@pytest.mark.parametrize("value", ["xyz" + "a" * 29, "arb" + "a" * 28, "arb" + "G" * 29])
def test_encode_rejects_a_non_canonical_id(value: str) -> None:
    with pytest.raises(DomainValidationError):
        to_hex128_client_id(value)


def test_known_venues_and_their_defaults() -> None:
    assert sorted(VENUES) == ["binance", "hyperliquid"]
    hl = venue_profile("hyperliquid")
    assert (hl.default_taker_fee_pct, hl.default_maker_fee_pct) == (
        Decimal("0.07"),
        Decimal("0.04"),
    )
    assert hl.ticker_source is TickerSource.ORDER_BOOK
    assert hl.clock_source is ClockSource.ORDER_BOOK
    assert hl.client_id_scheme is ClientIdScheme.HEX128
    assert hl.market_data_capabilities == ("fetch_ohlcv", "fetch_order_book")
    assert hl.supports_trading is False
    binance = venue_profile("binance")
    assert binance.default_taker_fee_pct == Decimal("0.1")
    assert binance.ticker_source is TickerSource.TICKER
    assert binance.clock_source is ClockSource.SERVER_TIME
    assert binance.client_id_scheme is ClientIdScheme.PLAIN
    assert binance.supports_trading is True


def test_unknown_venue_lists_the_supported_ones() -> None:
    with pytest.raises(UnknownVenueError, match=r"supported: \['binance', 'hyperliquid'\]"):
        venue_profile("kraken")
