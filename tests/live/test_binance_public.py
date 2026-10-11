"""Opt-in smoke test against Binance's real public API (no credentials).

Skipped unless ARBITRON_LIVE_EXCHANGE_TESTS=1. Never runs in ordinary CI
(plan §21.5). Use it to confirm the recorded contract fixtures still match the
live venue after a CCXT upgrade.
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


async def test_binance_public_market_data() -> None:
    clock = SystemClock()
    adapter = create_market_data(ArbitronConfig.model_validate(example_data()), clock)
    try:
        markets = await adapter.load_markets()
        candles = await adapter.fetch_ohlcv("BTC/USDT", Timeframe("5m"), limit=5)
        ticker = await adapter.fetch_ticker("BTC/USDT")
        offset = await measure_clock_offset(adapter, clock)
    finally:
        await adapter.close()

    assert markets["BTC/USDT"].active
    assert len(candles) == 5
    assert [c.is_closed for c in candles[:-1]] == [True] * 4
    assert ticker.bid is not None
    assert ticker.ask is not None
    assert abs(offset) < timedelta(seconds=5)
