"""Opt-in tests against the exchange sandbox (Binance spot testnet).

Skipped unless ARBITRON_SANDBOX_TESTS=1 and sandbox credentials are set
(ARBITRON_EXCHANGE_API_KEY / ARBITRON_EXCHANGE_API_SECRET, testnet keys only).
They place small orders on the sandbox. Never run in ordinary CI (plan §21.5).
The database fixtures are the integration ones.
"""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from arbitron.clocks import SystemClock
from arbitron.config.settings import ArbitronConfig, Secrets, load_config
from arbitron.exchange.ccxt_adapter import CcxtMarketData, create_market_data
from arbitron.exchange.ccxt_trading import CcxtTrading, create_trading
from tests.integration.conftest import database_url, engine, postgres_url, uow

EXAMPLE_CONFIG = Path(__file__).parents[2] / "configs" / "example.sandbox.toml"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    enabled = os.environ.get("ARBITRON_SANDBOX_TESTS") == "1"
    has_keys = Secrets().has_exchange_credentials
    skip = pytest.mark.skip(
        reason="set ARBITRON_SANDBOX_TESTS=1 and sandbox API keys to call the sandbox"
    )
    for item in items:
        if "tests/sandbox" in str(item.path):
            item.add_marker(pytest.mark.sandbox_exchange)
            if not (enabled and has_keys):
                item.add_marker(skip)


@pytest.fixture
def sandbox_config() -> ArbitronConfig:
    return load_config(EXAMPLE_CONFIG)


@pytest.fixture
async def trading(sandbox_config: ArbitronConfig) -> AsyncIterator[CcxtTrading]:
    adapter = create_trading(sandbox_config, SystemClock())
    try:
        yield adapter
    finally:
        await adapter.close()


@pytest.fixture
async def market_data(sandbox_config: ArbitronConfig) -> AsyncIterator[CcxtMarketData]:
    adapter = create_market_data(sandbox_config, SystemClock())
    try:
        yield adapter
    finally:
        await adapter.close()


__all__ = ["database_url", "engine", "postgres_url", "uow"]
