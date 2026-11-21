"""Opt-in smoke test against Hyperliquid's real public API (no credentials).

Skipped unless ARBITRON_LIVE_EXCHANGE_TESTS=1. Never runs in ordinary CI. Use
it to confirm the recorded Hyperliquid fixtures still match the live venue
after a CCXT upgrade.
"""

import os
from datetime import timedelta

import pytest

from arbitron.clocks import SystemClock
from arbitron.config.settings import ArbitronConfig
from arbitron.domain.market import Timeframe
from arbitron.exchange.ccxt_adapter import create_market_data
from arbitron.market.freshness import measure_clock_offset
from tests.unit.test_config import example_data

pytestmark = [
    pytest.mark.live_exchange,
    pytest.mark.skipif(
        os.environ.get("ARBITRON_LIVE_EXCHANGE_TESTS") != "1",
        reason="set ARBITRON_LIVE_EXCHANGE_TESTS=1 to call the live venue",
    ),
]


async def test_live_hyperliquid_market_data_is_usable() -> None:
    data = example_data()
    data["exchange"].update({"id": "hyperliquid", "symbols": ["BTC/USDC", "ETH/USDC"]})
    clock = SystemClock()
    adapter = create_market_data(ArbitronConfig.model_validate(data), clock)
    try:
        markets = await adapter.load_markets()
        ticker = await adapter.fetch_ticker("BTC/USDC")
        offset = await measure_clock_offset(adapter, clock)
        candles = await adapter.fetch_ohlcv("ETH/USDC", Timeframe("4h"), limit=50)
    finally:
        await adapter.close()

    assert {"BTC/USDC", "ETH/USDC"} <= set(markets)
    assert markets["BTC/USDC"].active
    assert ticker.bid is not None
    assert ticker.ask is not None
    assert ticker.bid < ticker.ask
    assert abs(offset) < timedelta(seconds=1)
    assert len(candles) == 50
