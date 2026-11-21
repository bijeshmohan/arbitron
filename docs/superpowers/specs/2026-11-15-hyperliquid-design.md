# Hyperliquid spot support: design

Status: approved by the operator 2026-11-21. Branch: `feature/hyperliquid`. No code is
written until this spec and the implementation plan are approved.

## 1. Goal and scope

Add Hyperliquid **spot** as a second supported venue, with the same safety
properties as the Binance adapter: paper and backtest first, then a testnet
qualification, then a soak. **Live mode stays impossible in this build.**

Decided by the operator (2026-11-15):

- Spot only, long-only, same sizing, risk, kill-switch and reconciliation rules
  as Binance.
- Approach A: a second adapter on the existing CCXT layer, with a per-venue
  profile, and the raw Hyperliquid info endpoint where CCXT lacks a call.
- Work happens on `feature/hyperliquid`, in a separate git worktree
  (`/home/bijesh/projects/arbitron-hyperliquid`), and must not disturb the
  running paper deployment (see section 9).

Out of scope: perpetuals, leverage, margin, live trading, any change to the
Binance adapter's behaviour, and any strategy decision.

## 2. What was verified, and what was not

Verified on 2026-11-15 against CCXT 4.5.85 and Hyperliquid's public API:

| Fact | Value |
|------|-------|
| CCXT `hyperliquid` flags | spot and swap, createOrder, cancelOrder, fetchOrder, fetchOpenOrders, fetchMyTrades, fetchBalance, fetchOrderBook, fetchOHLCV, sandbox: true |
| Ticker | `fetchTicker` is `"emulated"` (built from the order book) |
| Server time | no `fetchTime` |
| Fetch by client ID | `fetchOrderByClientOrderId` is `None` in CCXT |
| Spot liquidity, mainnet | BTC/USDC spread about 0.15 bps, about $480k bids and $600k asks in the top 5 levels; ETH/USDC about 1.1 bps, $310k and $360k; HYPE/USDC about 0.6 bps |
| Spot quotes | mostly USDC; a few USDT, USDE, USDH pairs with wider spreads (HYPE/USDT about 7.7 bps) |
| Fees (CCXT base tier) | taker 0.07%, maker 0.04% |
| Minimum order value | 10 (quote) |
| Testnet | exists, with a different market set (1,249 spot markets, many test-only quotes) |

**Not verified, to be settled by qualification (phase 2) on the testnet:**
whether fees are taken from the received or the quote asset; lookup of an order
by `cloid`; partial-fill and IOC-expiry reporting; the exact price-precision
rules (at most 5 significant digits, limited decimals per asset); what a duplicate
`cloid` returns; the testnet funding procedure. Nothing in this spec assumes an
answer to those. Every one of them gets a qualification test.

## 3. Venue profile

`SUPPORTED_VENUES = frozenset({"binance"})` becomes a registry of frozen
`VenueProfile` records. An unlisted venue is still refused at startup.

A profile holds:

- **Capabilities:** which CCXT flags the venue must provide natively, and which
  it may provide as `"emulated"`. For Hyperliquid only the ticker may be
  emulated (one order-book call). The others stay native-only.
- **Clock check:** Binance compares with server time. Hyperliquid has no
  server-time call, so it compares the local clock with the newest candle and
  trade timestamps, with a stated, looser tolerance. The profile names the method.
- **Client ID codec:** `encode(arb_id) -> venue_id` and `decode(venue_id) ->
  arb_id | None`.
- **Default fees** for paper and backtest, taker and maker, as venue defaults
  that the config can still override.
- **Order types** accepted (IOC limit only, as today).

The profile is data plus small pure functions. Adapter code reads it. It does
not branch on a venue name.

## 4. Client order IDs

Our ID is `arb` + 29 lowercase hex characters (32 characters, `domain/ids.py`).
Hyperliquid's `cloid` is `0x` + 32 hex characters (128 bits). The codec is
reversible and needs no stored mapping:

    encode: "arb" + h  ->  "0x" + "000" + h        (h = 29 hex chars)
    decode: "0x" + "000" + h  ->  "arb" + h
            anything else  ->  None  (a foreign order)

A venue order whose `cloid` does not decode is not ours. It goes through the
same unknown-order path as an unknown Binance order (reconciliation review).
The ID generator and the order state machine do not change. Only the adapter
edge translates.

## 5. Authenticated adapter (phase 2)

- **Credentials:** an **API wallet** (agent wallet) private key, which can place
  and cancel orders but cannot withdraw, plus the **main wallet's public
  address**. The main wallet's key never goes on the server. The operator
  approves the API wallet from the main wallet in a browser.
- **Secrets:** the private key is read from a secret file, never from the
  environment or the TOML, and is redacted from logs, in the same way as the
  Binance secrets.
- **Guards** (as `create_trading` has for Binance): refuse any mode but sandbox
  while live mode doesn't exist; refuse non-testnet URLs; refuse to start if the
  configured address equals the API wallet's own address (that would mean the
  main key is on the server).
- **Writes** are sent once and reads are retried, through the existing
  `VenueCaller`. IOC limit orders only, inside `max_slippage_pct`.
- **Lost-response recovery:** order status by `cloid` through CCXT's raw info
  call. "Unknown order" feeds the existing `NOT_PLACED` rule: two not-found
  lookups after the settle window.
- **Balances:** spot balances only. Spot and perp balances are separate on
  Hyperliquid, and perp balances are ignored.
- **Price precision:** the adapter rounds prices to the venue's rules and a
  qualification test proves them. A wrong price would be a venue rejection.
- **Errors:** venue error responses map to the existing `ExchangeError` types.
  Unmapped ones fail closed as `UnknownExchangeError`.

## 6. Market data, paper and backtest (phase 1, no keys)

- `market backfill` reads Hyperliquid candles. The API caps history at about
  5,000 candles per timeframe (about 2.3 years of 4h, about 7 months of 1h).
  Docs and the CLI output say so.
- Candles are stored under their own `exchange_id`, so Binance and Hyperliquid
  history never mix.
- Paper and backtest use the venue profile's default fees (0.07% taker). The
  paper venue's book-based fills are checked against the live Hyperliquid book.
- Quote asset comes from the market rules. Nothing outside docs and examples
  hard-codes USDT.
- Example configs `configs/example.hyperliquid.toml` (paper) and later
  `example.hyperliquid-testnet.toml`.

## 7. Phases

Each phase ends with a stop for the operator's review, and is committed on the
feature branch.

1. **Venue profile, data, paper, backtest. No keys.** Done when: Hyperliquid is
   selectable for paper and backtest; BTC/USDC and ETH/USDC candles backfill; the
   profile, ID codec and capability handling have unit and contract tests;
   recorded public responses are fixtures; a documented comparison of paper
   fills against the live book exists; the full test suite, lint and
   type-check pass.
2. **Authenticated testnet adapter and qualification.** Needs the operator's
   testnet wallet and API wallet (a step-by-step guide is part of this phase).
   Done when: `sandbox` commands work for Hyperliquid; the order lifecycle,
   lost-response, duplicate-`cloid`, expiry and partial-fill probes have run;
   every item in "not verified" (section 2) has a recorded answer in
   `docs/hyperliquid-qualification.md`; contract tests on recorded responses and
   an opt-in sandbox suite pass.
3. **Runbooks, review document, soak.** Update the runbooks and
   `docs/live-readiness-review.md`; run a paper soak on Hyperliquid sized to its
   risks, judged by criteria fixed beforehand. Done when the soak report passes
   and the residual-risk list is updated.

## 8. Testing

- **Unit:** ID codec (round trip, wrong length, wrong prefix, upper case),
  profile validation, price rounding, error mapping.
- **Contract:** recorded Hyperliquid responses (market data in phase 1, orders in
  phase 2). Fixtures never contain private keys or addresses of real accounts.
- **Failure injection:** lost response, duplicate `cloid`, venue "unknown
  order", rejection, timeout, in the style of `tests/failure/`.
- **Sandbox (opt-in):** `ARBITRON_SANDBOX_TESTS=1`, places testnet orders.
- **Regression:** the full existing suite must pass unchanged, which proves the
  Binance behaviour did not move.

## 9. Operating constraints during development

- **Do not disturb the running deployment.** The paper stack (project
  `arbitron`, containers `arbitron-*`) is running the forward test of
  `trend_filter` on ETH/USDT 4h, started 2026-11-14 14:49:12Z, with a review on
  2026-12-12. During development:
  - work only in the worktree; the main checkout stays on `main`;
  - never run `docker compose` against `deploy/compose.yaml` from the worktree
    (it shares the project name `arbitron`);
  - never rebuild or recreate the `arbitron-*` containers or edit
    `deploy/arbitron.toml` from this work;
  - use the separate dev PostgreSQL (`deploy-postgres-1`, localhost:5432) only
    through a separate database name (for example `arbitron_hl`) for any
    migration or backfill, not the research data's `arbitron` database;
  - a migration on this branch is not applied to the deployed database.
- **Merging** to `main` and redeploying are separate decisions the operator
  makes after phase 3, not part of this work.
- **Commits and pushes** happen only when the operator asks (the feature branch
  is pushed on request).

## 10. Risks

| Risk | Handling |
|------|----------|
| Wrapped assets (UBTC, UETH) carry bridge and issuer risk Binance coins don't | recorded as a residual risk; the dedicated wallet holds only what the bot uses |
| Chain or venue halt, smart-contract risk | fail closed on stale data and unhealthy venue, as now; documented in the exchange-outage runbook |
| A signing key on the server | API wallet only (no withdrawal); secret file; guard against the main key |
| Wrong price precision causes rejections | qualification proves rounding; rejections alert and are counted by the soak |
| Fee asset or partial-fill semantics differ from Binance | not assumed; settled in qualification before any adapter claims correctness |
| Testnet differs from mainnet (markets, liquidity) | qualification is for order flow, not prices; stated in the report |
| Thin spot books outside BTC, ETH, HYPE | symbol allowlist stays in config; start with BTC/USDC and ETH/USDC |
| Short candle history limits backtests | docs say so; Binance history remains the longer proxy, labelled as such |
| Tax and legal status in India | operator to confirm with a professional; out of scope for the software |

## 11. Decisions on the open questions (operator, 2026-11-21)

1. Start with BTC/USDC and ETH/USDC.
2. No Hyperliquid paper strategy is chosen as part of this work. It waits for
   the research results. The venue work needs no strategy.
