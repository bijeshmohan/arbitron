# Sandbox qualification: Binance spot testnet (Phase 11)

This document records what was checked against the exchange sandbox, how to
repeat the checks, and what the sandbox cannot show. No live keys were used.
Live order paths stay disabled until the Phase 12 review.

- **Venue:** Binance spot testnet. REST `https://testnet.binance.vision/api`.
- **Qualified on:** 2026-10-23, CCXT 4.5.85, symbol BTC/USDT.
- **Credentials:** testnet HMAC keys, created at testnet.binance.vision with a
  GitHub login. They are kept in `ARBITRON_EXCHANGE_API_KEY` and
  `ARBITRON_EXCHANGE_API_SECRET` and are separate from any live keys. Testnet
  keys cannot be restricted: the account reports `canWithdraw: true`. That
  doesn't matter on the testnet, because the funds are virtual and cannot
  leave it.

## How to repeat it

```bash
cp configs/example.sandbox.toml configs/sandbox.toml
uv run arbitron sandbox check configs/sandbox.toml          # read-only
uv run arbitron sandbox qualify configs/sandbox.toml --yes  # places tiny orders
ARBITRON_SANDBOX_TESTS=1 uv run pytest tests/sandbox        # + reconciliation (needs Docker)
```

`sandbox qualify` uses orders of about twice the minimum notional, roughly
10 USDT. It cancels anything it leaves resting.

## Safety gates for the authenticated adapter

`arbitron.exchange.ccxt_trading.create_trading` is the only code that
authenticates to a venue. It refuses to build the adapter unless:

1. `app.mode = "sandbox"` and `exchange.sandbox = true`. Live mode cannot be
   configured at all yet.
2. CCXT's public and private API URLs both start with
   `https://testnet.binance.vision/`. This is checked after
   `set_sandbox_mode`, as defense in depth.
3. The venue natively supports every call the adapter relies on: balances,
   order queries, open orders, own trades, create and cancel.

Order submission and cancellation are sent exactly once and never retried.
Only reads are retried.

## What was verified

| Area | Result on the testnet |
|------|-----------------------|
| Clock | Venue minus local time was 37–148 ms, within `max_clock_skew_ms`. |
| Market rules | BTC/USDT: amount step 0.00001, price tick 0.01, minimum amount 0.00001, minimum notional 5 USDT. These match the filters the paper venue enforces. |
| Precision | An off-grid amount (for example 0.000145) is refused before sending. CCXT would otherwise truncate it silently, so the venue order would differ from the recorded intent. |
| Minimums | An order below the minimum notional is rejected by the venue with `-1013 Filter failure: NOTIONAL` and classified as a definitive rejection. |
| Client IDs | The canonical 32-character `arb…` ID is accepted as `newClientOrderId` and returned on create, query and open orders. Lookup by client ID (`origClientOrderId`) works. |
| Duplicate client ID | Re-sending a client ID while that order is open returns `-2010 Duplicate order sent`. This is classified as an unknown outcome (reconcile by client ID), not a rejection. |
| Resting order | A GTC buy 10% below the bid rests as `NEW` (internal status `OPEN`) and is visible by exchange ID, by client ID and in open orders. |
| Cancellation | Cancel returns `CANCELED`, and re-fetching confirms it. Cancelling again (`-2011 Unknown order sent`) reports the order's final state instead of failing. |
| IOC | An IOC limit that can't fill returns `EXPIRED` with nothing filled. A marketable IOC buy and the matching sell return `FILLED`. |
| Fills | The account's own trades (`myTrades`) for each order add up exactly to the order's executed quantity and carry venue trade IDs. |
| Unknown orders | Looking up a client ID or order ID that never existed returns "not found" (`-2013`), never an error. |
| Reconciliation: lost response | An order the venue accepted but the bot never heard about (`SUBMISSION_UNKNOWN`) is found by client ID. It then moves to `FILLED`, with its fills recorded and the position updated. |
| Reconciliation: never sent | An unknown submission the venue never received becomes `NOT_PLACED` after the settle window and two consecutive not-found lookups. |

Recorded venue responses in `tests/fixtures/binance/testnet/` are replayed
in CI through real CCXT parsing (`tests/contract/test_binance_trading.py`).

## Fixes made because of the sandbox

1. **`BadRequest` is now a definitive rejection.** Binance reports order filter
   failures (`-1013`) as HTTP 400, which CCXT raises as `BadRequest`. These were
   classified as "unknown outcome", so an order below the minimums would have
   caused a `SUBMISSION_UNKNOWN`, a critical alert and a reconciliation cycle.
   The venue only sends this error after refusing the request, so the order
   cannot exist (`exchange/errors.py`).
2. **A duplicate client order ID is now an unknown outcome.** CCXT raises
   Binance's `-2010 Duplicate order sent` as a plain `InvalidOrder`, which reads
   as "definitely not placed". An order with that ID is open, though. A
   rejection would have orphaned it, so the adapter re-raises it as
   `UnknownExchangeError`. The order manager never re-sends a client ID, so
   this is a safety net, not an expected path.
3. **Cancel responses report our client ID.** Binance's cancel response puts a
   new random value in `clientOrderId` and moves ours to `origClientOrderId`.
   CCXT surfaces the random one, and the reconciler would have read it as a
   client ID mismatch. The mapping now prefers `origClientOrderId`.
4. **Trade history is fetched in windows of 24 hours or less.** Binance spot
   rejects `myTrades` ranges longer than 24 hours (`-1127 More than 24 hours
   between startTime and endTime`, observed), but the reconciler's first
   window is 24 h plus 10 min of overlap. The adapter now splits ranges and
   pages within full windows.
5. **Open orders are queried per symbol.** An account-wide query costs ten
   times the rate-limit weight on Binance spot, and CCXT refuses it by default.

## Limitations and unsupported behavior

- **Fees cannot be qualified.** The testnet charges zero commission
  (`commissionRates` are all 0), so every fill reports a fee of 0. Fee
  accounting is covered only by unit tests and the paper venue's model.
  **BNB fee discounts:** if "pay fees with BNB" were on for a live account,
  fills would report fees in BNB. The accounting refuses those (`fee in BNB is
  not supported`) and the order goes to reconciliation. Keep the BNB fee
  option off, or add BNB fee accounting before going live.
- **Prices and liquidity are not realistic.** The testnet book is deep and
  tight, so slippage and partial fills are rarely seen. A partial fill on an
  IOC order was not observed. That path is covered by the status-mapping unit
  tests and the paper venue only.
- **Monthly resets.** The testnet is wiped about once a month without notice,
  deleting orders, trades and balances. After a reset, reconciliation of any
  earlier sandbox session reports its orders as missing. Start sandbox runs
  from a fresh database.
- **Pre-funded balances and foreign trades are discrepancies, by design.**
  Every testnet account starts with balances in hundreds of assets, including
  1 BTC. The reconciler compares the venue's base-asset balance with the
  bot's position, and it flags any trade on an order the bot does not know
  about (`sandbox qualify` runs, manual trades). Both hold the kill switch in
  `RECOVERY_REVIEW`. That is the intended fail-closed behavior. It also means
  the bot needs **a dedicated account that starts flat in every configured
  base asset**, on the testnet and later on live. See the Phase 12 review
  question below.
- **Only `/api` endpoints exist.** There are no `/sapi` endpoints, so there are
  no wallet, deposit, withdrawal or sub-account calls. The bot uses none of
  them.
- **Self-trade prevention.** The default mode is `EXPIRE_MAKER`. A bot IOC
  that crosses its own resting order would expire that order
  (`EXPIRED_IN_MATCH`, mapped to `EXPIRED` or `PARTIALLY_FILLED_CLOSED`).
  All bot orders are IOC today, so this cannot happen.
- **Klines.** The testnet's candles are not live market data. Strategies and
  data-quality gates should use production public data, as paper mode does.
  Sandbox mode is for order mechanics only.
- **Open orders outside the allowlist** are not queried, so the reconciler
  does not see them.

## Open question for the Phase 12 review

Reconciliation holds the kill switch in review whenever the account holds
base assets the bot didn't buy, or shows trades it didn't place. Before live
trading, decide between:

1. **A dedicated account or sub-account** that starts flat in every configured
   base asset and is used only by the bot. This needs no code change.
2. **A recorded opening balance per asset**, with the reconciler comparing
   the venue balance against that baseline plus the bot's position. This
   needs a code change.
