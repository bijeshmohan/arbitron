"""Record public Hyperliquid ``POST /info`` responses as contract-test fixtures.

Run by hand (needs network, no credentials):

    uv run python tests/fixtures/hyperliquid/record.py

Each response is saved as ``<type>[_<coin>].json`` next to this file. Only
public market data is requested; no address or key is involved.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import ccxt.async_support as ccxt_async

HERE = Path(__file__).parent


def fixture_name(body: dict[str, Any]) -> str:
    """The file a request maps to; the contract harness uses the same rule."""
    kind = str(body["type"])
    coin = body.get("coin") or body.get("req", {}).get("coin")
    return f"{kind}_{coin}.json" if coin else f"{kind}.json"


async def main() -> None:
    exchange: Any = ccxt_async.hyperliquid(
        {"enableRateLimit": True, "options": {"fetchMarkets": {"types": ["spot"]}}}
    )
    original = exchange.fetch

    async def recording_fetch(
        url: str, method: str = "GET", headers: Any = None, body: Any = None
    ) -> Any:
        response = await original(url, method, headers, body)
        name = fixture_name(json.loads(body))
        (HERE / name).write_text(json.dumps(response, indent=1) + "\n")
        print("recorded", name)
        return response

    exchange.fetch = recording_fetch
    try:
        await exchange.load_markets()
        await exchange.fetch_order_book("BTC/USDC", 1)
        await exchange.fetch_ohlcv("BTC/USDC", "4h", limit=10)
    finally:
        await exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
