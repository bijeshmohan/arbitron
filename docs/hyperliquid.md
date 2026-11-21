# Hyperliquid spot

Hyperliquid spot is the second supported venue. In this build it supports
**market data, backtests and paper trading only**. There are no keys and no
orders: authenticated trading is refused with "authenticated trading is not
implemented for hyperliquid in this build". The testnet adapter and its
qualification come in phase 2 of the design
([spec](superpowers/specs/2026-11-15-hyperliquid-design.md)).

Start from [`configs/example.hyperliquid.toml`](../configs/example.hyperliquid.toml).

## How it differs from Binance

| | Binance spot | Hyperliquid spot |
|---|---|---|
| Quote currency in the examples | USDT | **USDC** |
| BTC and ETH | native coins | **Unit-bridged wrapped assets** (UBTC, UETH): extra bridge and issuer risk |
| Default fee (paper, backtest) | 0.1% | **0.07%** taker, 0.04% maker (CCXT base tier, checked 2026-11-15) |
| Minimum order value | per market | 10 USDC |
| Ticker | the venue's ticker | **top of the order book** (CCXT's emulated ticker has no bid or ask) |
| Clock check | server time | **the order book's venue timestamp** (no server-time endpoint), same `max_clock_skew_ms` |
| Candle history | years | about **5,000 candles per timeframe**: roughly 2.3 years of 4h, 7 months of 1h |
| Testnet | order flow, different balances | order flow only: a different market list and test-only tokens |

An explicit `fee_pct` in `[paper]` or `[backtest]` (including `"0"`) always
overrides the venue default. `arbitron check-config` prints the fees in use.

## Short history

`market backfill` with a start earlier than the venue's history stores what
exists and reports the gap from the start to the first available candle. A
4h backfill from 2024-01-01 starts at the venue's earliest 4h candle
(2025-02-03 when checked on 2026-11-21). Backtests on Hyperliquid data are
therefore much shorter than on Binance. Longer Binance history can stand in
for the same assets, but label it as a proxy: the price, the wrapped asset and
the fees differ.

## Separate data

Candles, markets and paper accounts are keyed by venue, so Binance and
Hyperliquid history never mix. Use a separate paper `account` per venue (the
example uses `hyperliquid`).

## Checks

- Contract tests on recorded public responses: `tests/contract/test_hyperliquid_market_data.py`
  (fixtures in `tests/fixtures/hyperliquid/`, re-recorded with `record.py` there).
- Opt-in live check: `ARBITRON_LIVE_EXCHANGE_TESTS=1 uv run pytest tests/live/test_hyperliquid_public.py`.

## Phase 1 verification

To be filled in by the manual verification run.
