# Live-readiness review (Phase 12)

This is a **review gate, not a deployment.** Nothing here enables live
trading. Live mode cannot be configured in this build. Turning it on later
needs a separate, reviewed code change (the live gates in plan §18) after this
checklist is signed. The coding agent doesn't set financial limits or enable
any live order path (plan §22).

Status key: **done** (evidence linked), **in progress**, **operator**
(needs a human decision or action), **blocker** (must be resolved before any
live change is even proposed).

## Checklist

| # | Item (plan §22, Phase 12) | Status | Evidence / what remains |
|---|---------------------------|--------|-------------------------|
| 1 | Sustained paper soak with a predefined duration and thresholds | in progress | 14 days. Criteria fixed in advance in [soak-test.md](soak-test.md) and scored by `arbitron soak report`. The start time and drill log go in that file. |
| 2 | Strategy evaluation methodology and out-of-sample results | **blocker** | No strategy has been chosen for live. `sma_cross` is a software integration fixture and is refused in live mode (`tests/unit/test_strategy.py::test_fixture_strategy_refused_in_live`). Method available: backfilled history, an in-sample sweep, then an out-of-sample check (`backtest sweep --split`, README → Backtesting). Before any live proposal, the operator must pick a strategy, record the out-of-sample results, and accept that they are not evidence of future returns. |
| 3 | Risk limits and kill-switch behavior | operator | Behavior: the kill switch is durable. It trips automatically on loss or drawdown limits, authentication failure and the restart limit, and goes to review on discrepancies. Only a named operator can re-arm, from the CLI ([runbooks/kill-switch.md](runbooks/kill-switch.md), `docs/failure-matrix.md` #4, #14, #17). The **values** (order notional, exposures, daily loss, drawdown, reserve) are still example values and must be set by the operator. Also drilled during the soak (day 6). |
| 4 | Exchange account permissions and key storage | operator | Decided 2026-10-24: a **dedicated Binance sub-account** used only by the bot, starting flat in every configured base asset. Key: spot trading only, no withdrawals or transfers, restricted to the host's IP, stored as a secret file (never in env or source) ([runbooks/credential-rotation.md](runbooks/credential-rotation.md)). Still to confirm: account eligibility and KYC in India, and that the sub-account supports API keys with these restrictions. |
| 5 | Recovery drill and database restore drill | in progress | Scheduled during the soak: graceful restart (day 2), hard kill (day 4), network outage (day 8), `restore.sh verify` (day 9). Results go in the soak log. Already covered by automated tests: failure matrix rows #1–#17 (`tests/failure/`), and lost response and never-sent orders against the real sandbox ([sandbox-qualification.md](sandbox-qualification.md)). |
| 6 | Alert routing and operator runbooks | done, with gaps | Telegram, critical events only, deduplicated and rate-limited. Runbooks: [runbooks/](runbooks/README.md), covering the 8 situations in plan §23. Gaps are listed under residual risks (backup failures and alert delivery aren't monitored). |
| 7 | Live mode cannot start without all explicit gates | done | Live mode is refused outright: `app.mode = "live"` and any `[live]` flag fail validation (`tests/unit/test_config.py::test_live_mode_is_refused`, `test_live_flags_refused_outside_live_mode`). The only authenticated adapter refuses anything but sandbox mode with testnet URLs (`tests/contract/test_binance_trading.py::TestSandboxOnly`). The plan §18 gates (explicit mode, acknowledgement, keys, approved config, healthy reconciliation, armed switch) are **not implemented**, because live mode doesn't exist yet. Implementing them is the future change this review would approve. |
| 8 | Human approval before enabling any live order path | operator | Sign-off below. Unsigned. |

## Release checklist (plan §26)

| Area | Item | Status |
|------|------|--------|
| Correctness | UTC timestamps, Decimal arithmetic, deterministic dedupe, central order state machine, immutable fills | done: domain checks and unit tests (Phases 1–2) |
| Correctness | Partial fills and cancel/fill races | done on paper and in tests; a **partial fill was not observed on the sandbox** (residual risk 2) |
| Safety | Live disabled by default; risk approval for every order | done (item 7; the risk gate in Phase 5) |
| Safety | Fail closed on stale data, DB failure, unhealthy venue, unresolved reconciliation | done: failure matrix #5, #11, #14, #16 |
| Safety | Durable kill switch, audited operator actions | done: `kill_switch_events`, actor on every change |
| Safety | No withdrawal permission | operator: key setup (item 4). Testnet keys cannot be restricted. |
| Safety | No blind retry of ambiguous submissions | done: writes are sent once (`exchange/calls.py`); failure matrix #1–#3 |
| Recovery | Startup and periodic reconciliation; review on unknown orders or discrepancies | done (Phase 7; sandbox qualified) |
| Recovery | Restart and restore drills | in progress (soak) |
| Operations | Health and readiness, heartbeats, metrics, redacted logs, single instance | done (Phases 8, 10) |
| Operations | Actionable alerts; backup, restore and incident runbooks | done, with gaps (item 6) |
| Validation | Unit, integration, contract and failure-injection tests | done: CI on every push |
| Validation | Backtest assumptions documented | done (plan §17.1.1, README) |
| Validation | Paper soak completed | in progress |
| Validation | Sandbox qualification for the chosen venue | done (Phase 11) |
| Validation | Human live-readiness review | operator |

## Residual risks

Known and accepted only if the signer says so:

1. **Fees aren't verified against the venue.** The testnet charges zero
   commission. Paying fees in BNB would send every fill to reconciliation.
   Keep "pay fees with BNB" off on the live account.
2. **Partial fills and real slippage** have only been seen in simulation. The
   sandbox book is deep, and live liquidity behaves differently.
3. **Single host with no failover.** If the host goes down, trading stops.
   Recovery is by restart and reconciliation.
4. **Backup failures don't alert**, and dumps stay on the host until copied
   off by hand (weekly, per [runbooks/database-restore.md](runbooks/database-restore.md)).
5. **Alert delivery isn't recorded.** Telegram is the only channel, and a
   Telegram outage would go unnoticed.
6. **Open orders on symbols outside the allowlist** aren't queried, so they
   aren't seen. This is mitigated by the dedicated account (item 4).
7. **Records lost in a database restore.** Activity after the dump exists
   only on the venue and must be reconciled by hand before re-arming.
8. **Clock.** Only the offset to the venue's time is checked
   (`max_clock_skew_ms`). Host NTP isn't monitored.
9. **Regulation and tax.** Crypto trading from India has reporting and tax
   obligations (for example TDS on virtual digital asset transfers). The
   operator should confirm these with a professional. They are out of scope
   for this software.
10. **Strategy risk.** No backtest, paper result or sandbox run predicts
    returns. Losses are limited only by the risk limits the operator sets.

## Sign-off

Signing means: the items above marked done have been checked, the operator
items are resolved as described, each residual risk is accepted, and a code
change implementing the plan §18 live gates may be **proposed** for review.
It does not enable trading.

| | |
|---|---|
| Soak report result and dates | |
| Strategy, version and out-of-sample summary | |
| Risk limit values (set by the operator) | |
| Exchange account and key restrictions verified | |
| Residual risks accepted (list numbers) | |
| Operator name | |
| Date (UTC) | |
| Signature / commit that records this | |
