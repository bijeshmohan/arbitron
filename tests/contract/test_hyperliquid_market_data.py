"""Contract tests: arbitron's market-data adapter on Hyperliquid spot, over real CCXT parsing."""

from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from arbitron.clocks import ManualClock
from arbitron.config.settings import ArbitronConfig
from arbitron.domain.market import Timeframe
from arbitron.exchange.ccxt_adapter import CcxtMarketData, create_market_data, venue_options
from arbitron.exchange.mapping import datetime_from_ms, ticker_from_order_book
from arbitron.ports import errors as venue
from tests.contract.conftest import (
    HyperliquidTransport,
    hyperliquid_with_transport,
    load_hl_fixture,
)
from tests.unit.test_config import example_data

BOOK = load_hl_fixture("l2Book_@142.json")
BOOK_TIME = datetime_from_ms(BOOK["time"], "book time")
CANDLES = load_hl_fixture("candleSnapshot_@142.json")
# After every recorded candle has closed, so all of them count as closed.
NOW = datetime_from_ms(CANDLES[-1]["T"], "candle close") + timedelta(minutes=1)


def hl_config() -> ArbitronConfig:
    data = example_data()
    data["exchange"].update({"id": "hyperliquid", "symbols": ["BTC/USDC", "ETH/USDC"]})
    data["strategy"]["timeframe"] = "4h"
    return ArbitronConfig.model_validate(data)


@pytest.fixture
def transport_and_adapter() -> tuple[HyperliquidTransport, CcxtMarketData]:
    exchange, transport = hyperliquid_with_transport(dict(venue_options("hyperliquid")))
    adapter = create_market_data(hl_config(), ManualClock(NOW), exchange=exchange)
    return transport, adapter


@pytest.fixture
async def hl(
    transport_and_adapter: tuple[HyperliquidTransport, CcxtMarketData],
) -> AsyncIterator[CcxtMarketData]:
    _, adapter = transport_and_adapter
    await adapter.load_markets()
    yield adapter
    await adapter.close()


async def test_markets_load_btc_and_eth_usdc_as_spot(hl: CcxtMarketData) -> None:
    markets = await hl.load_markets()

    btc = markets["BTC/USDC"]
    assert (btc.exchange_id, btc.base, btc.quote, btc.min_cost) == (
        "hyperliquid",
        "BTC",
        "USDC",
        Decimal(10),
    )
    assert "ETH/USDC" in markets


async def test_ticker_is_the_top_of_the_book(hl: CcxtMarketData) -> None:
    ticker = await hl.fetch_ticker("BTC/USDC")

    assert ticker.bid == Decimal(BOOK["levels"][0][0]["px"])
    assert ticker.ask == Decimal(BOOK["levels"][1][0]["px"])
    assert ticker.timestamp == BOOK_TIME


async def test_empty_ask_side_gives_no_ask_not_a_crash(
    transport_and_adapter: tuple[HyperliquidTransport, CcxtMarketData], hl: CcxtMarketData
) -> None:
    transport, _ = transport_and_adapter
    transport.use("l2Book", "l2Book_@142_no_asks.json")

    ticker = await hl.fetch_ticker("BTC/USDC")

    assert ticker.ask is None
    assert ticker.bid == Decimal(BOOK["levels"][0][0]["px"])


async def test_clock_comes_from_the_order_book_timestamp(hl: CcxtMarketData) -> None:
    assert await hl.fetch_time() == BOOK_TIME


async def test_candles_are_stored_under_hyperliquid(hl: CcxtMarketData) -> None:
    candles = await hl.fetch_ohlcv("BTC/USDC", Timeframe("4h"), limit=10)

    assert len(candles) == 10  # CCXT trims the recorded 11 to the requested limit
    assert {c.exchange_id for c in candles} == {"hyperliquid"}
    assert candles[-1].close == Decimal(CANDLES[-1]["c"])


def test_order_book_without_timestamp_is_invalid() -> None:
    raw: dict[str, Any] = {"bids": [[1.0, 1.0]], "asks": [[2.0, 1.0]], "timestamp": None}

    with pytest.raises(venue.InvalidVenueData):
        ticker_from_order_book(raw, exchange_id="hyperliquid", symbol="BTC/USDC")
