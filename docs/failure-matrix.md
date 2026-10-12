# Failure matrix

Required by plan §21.4 before Phase 7's failure-injection tests are written.
It states, for each failure the bot must survive:

- what it does to the order state;
- whether new orders are blocked, and how widely;
- the alert severity;
- whether recovery is automatic or needs an operator.

**Status:** approved by the operator on 2026-10-12. Every item marked
**DECISION** was accepted as proposed:

- settle window 30 s, plus two consecutive not-found checks;
- escalation after 15 minutes unresolved;
- discrepancies move the kill switch to `RECOVERY_REVIEW`;
- an authentication failure trips the kill switch.

## How reconciliation works (proposed)

Reconciliation compares the bot's own records (`orders`, `fills`, `positions`)
with the venue's (the paper venue's tables now; Binance later). It runs:

- **at startup**, before any new order can be placed (plan §14.1);
- **periodically**, every `execution.reconciliation_interval_seconds` (60 s in the
  example config);
- **straight after any ambiguous submission**.

Each run does the following, in order:

1. **Interrupted submissions.** Orders still in `SUBMITTING` at startup became
   ambiguous when the process stopped mid-submission. They move to
   `SUBMISSION_UNKNOWN`.
2. **Unknown submissions.** Each `SUBMISSION_UNKNOWN` order is looked up on the
   venue by its client order ID:
   - **Found:** apply the venue's state and fills, exactly as if the response
     had arrived. Recovery is automatic.
   - **Not found:** it becomes `NOT_PLACED` only once the order is older than
     the settle window **and** the venue reported it missing on two
     consecutive checks. Until then it stays unknown and keeps blocking new
     orders. **DECISION:** settle window 30 s and two checks.
   - **Still unresolved after the maximum age** → `RECONCILIATION_REQUIRED`,
     a critical event, and an operator must resolve it. **DECISION:** maximum
     age 15 minutes.
3. **Orders needing reconciliation.** Each `RECONCILIATION_REQUIRED` order is
   re-fetched from the venue. If the venue state, the venue's trades and the
   local fills now agree, the state is applied automatically. If not, it stays
   blocked for the operator.
4. **Fill sync.** For each configured symbol, the venue's trades are fetched
   from slightly before the last reconciliation (overlapping windows; plan
   §14.2). A trade that belongs to a known local order but is missing from
   `fills` is a **deterministic repair**: insert it, update the position, and
   record a warning event (plan §14.3). A trade with no matching local order is
   a discrepancy.
5. **Holdings check.** For each configured symbol, the venue's base-asset
   balance is compared with the bot's projected position quantity.
   - Paper: they must match exactly.
   - **DECISION:** a live venue would need a dust tolerance; that is deferred
     until live, so for now any mismatch is a discrepancy.
   - The quote balance is not checked yet: that needs a deposit/withdrawal
     ledger, which only a live venue has.
6. **Unknown venue orders.** Any open order on the venue whose client order
   ID has no local order is a discrepancy.

**Discrepancies** are anything that can't be repaired deterministically. Each
one does all of the following:

- records a critical `reconciliation.*` system event with the details;
- leaves new orders blocked: the snapshot's `reconciliation_clean` is false,
  so the risk gate rejects every order, exits included;
- **DECISION:** moves the kill switch to `RECOVERY_REVIEW` if it is `ARMED`.
  Resuming then needs `killswitch arm` by a named operator after review. This
  makes the block survive restarts and requires an explicit human decision.

**Repair policy (plan §14.3):** the bot never creates a compensating trade, and
never rewrites recorded fills or positions to match the venue. The only
automatic repairs are applying a verified venue state to an unknown or
unresolved order, and inserting a verified missing fill for a known order.

## The matrix

| # | Failure | Detected by | Resulting order state | New orders blocked? | Alert | Recovery |
|---|---------|-------------|-----------------------|---------------------|-------|----------|
| 1 | Network timeout before the request reaches the venue | submission raises `RequestTimeout` / `NetworkTransient` | `SUBMISSION_UNKNOWN`, then `NOT_PLACED` once the settle window has passed and the order is missing on 2 checks | Yes, all, until resolved | critical at submission; info on resolution | automatic |
| 2 | Timeout after the venue accepted the order, before the response arrives | as #1 | `SUBMISSION_UNKNOWN`, then the venue's state (e.g. `FILLED`) with its fills and position | Yes, until resolved | critical, then info | automatic |
| 3 | Rate limited on submission | `RateLimited` | `SUBMISSION_UNKNOWN` (a 429 usually means the order was not placed, but this isn't assumed), then as #1/#2 | Yes, until resolved | critical | automatic |
| 3b | Rate limited on reads | `RateLimited`, retried with backoff | none | only if data goes stale (quality gate) | none per retry; warning if the cycle fails | automatic |
| 4 | Authentication failure on submission | `AuthenticationFailure` | `EXCHANGE_REJECTED` (a definitive refusal) | yes, via the failed check; plus the kill switch is tripped, because bad keys won't fix themselves | critical | operator (fix the keys, review, arm) |
| 5 | Database unavailable | any repository call raises | unchanged: no state is written while the database is down | Yes. The cycle fails, nothing is submitted, and data goes stale | cycle error (logged; Phase 8 alerting) | automatic once the database is back; startup reconciliation then runs |
| 6 | Process killed during submission | at startup, orders found in `SUBMITTING` | `SUBMISSION_UNKNOWN`, then as #1/#2 | Yes, until resolved | critical | automatic |
| 6b | Cancelled while awaiting the venue (Ctrl-C, shutdown) | `CancelledError` in the order manager (already handled in Phase 6) | `SUBMISSION_UNKNOWN`, then as #1/#2 | Yes, until resolved | critical | automatic |
| 7 | Duplicate candle or event | candle upsert identity; signal and intent dedupe keys | unchanged (idempotent) | No | none | automatic |
| 8 | Partial fill then the remainder cancelled/expired (IOC) | venue reports filled < amount | `PARTIALLY_FILLED_CLOSED` with partial fills and position | No | info | automatic |
| 9 | Cancel/fill race (cancel acknowledged, fills still arrive) | IOC orders are final on submission, so this can't occur on paper. The handling exists for live: fill sync step 4 | for a terminal order: a late fill on a known order is inserted (deterministic repair); a state contradiction → `RECONCILIATION_REQUIRED` | only on contradiction | warning (repair) / critical (contradiction) | automatic / operator |
| 10 | Missing trade-history window (venue history shorter than the gap) | trades the venue no longer returns; the holdings check then disagrees | unchanged; discrepancy recorded | Yes: discrepancy → kill switch review | critical | operator |
| 11 | Stale market data | quality gate (Phase 3) | none created | Yes, all orders (`MARKET_DATA_STALE`) | critical event per blocked strategy run | automatic once data is fresh |
| 12 | Unknown order on the venue | step 6: venue open order with an unknown client ID | none locally | Yes: discrepancy → kill switch review | critical | operator |
| 13 | Risk engine raises | `decide()` catches it (Phase 5) | `RISK_REJECTED` (`RISK_STATE_UNAVAILABLE`) | that order only | warning | automatic (next signal) |
| 14 | Reconciliation disagreement (venue fills ≠ local fills, holdings ≠ position, contradictory state) | steps 3–5 | `RECONCILIATION_REQUIRED` (order-level) and/or a discrepancy (account-level) | Yes: discrepancy → kill switch review | critical | operator |
| 15 | Instance lock lost (database connection dropped) | `InstanceLock.check()` before each cycle | unchanged | Yes: the cycle aborts and the session stops; another instance may now hold the lock | critical | operator restarts; startup reconciliation runs |
| 16 | Venue unreachable during reconciliation | `VenueError` from reads | unchanged | Yes: reconciliation can't complete, so it doesn't count as clean | warning per run; critical after the maximum age (as #2) | automatic when the venue returns |
| 17 | Daily loss or drawdown breached | snapshot builder (Phase 6) | none | Yes: kill switch tripped | critical | operator (review, arm) |

## Not covered yet

- **Quote-balance reconciliation:** needs a deposit/withdrawal ledger (live
  only). It is deferred to live readiness.
- **Alert delivery:** critical events are recorded now; Phase 8 sends them to
  an operator channel.
- **Kill-switch policy for open orders (§24.8):** moot while all orders are IOC.
