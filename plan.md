# Autonomous Crypto Trading Bot --- Implementation Plan

**Status:** Implementation specification\
**Project:** `arbitron`\
**Language:** Python 3.12+\
**Package manager:** `uv`\
**Primary exchange integration:** CCXT\
**Initial operating mode:** Backtest and paper trading only\
**Production posture:** Fail closed; live trading disabled unless
explicitly configured and validated.

------------------------------------------------------------------------

## 1. Mission and success criteria

Build a modular, autonomous crypto-trading application that can ingest
market data, evaluate one or more deterministic strategies, enforce
independent risk limits, execute and track orders through CCXT, persist
its state, recover after restarts, and alert an operator only when
attention is warranted.

The system must optimize for **correctness, auditability, operational
safety, and recoverability** before strategy sophistication or trading
frequency.

### 1.1 Functional goals

-   [ ] Fetch and normalize market data through CCXT.
-   [ ] Run strategies behind a stable, exchange-independent interface.
-   [ ] Convert strategy signals into explicit order intents.
-   [ ] Validate every intent through a deterministic risk engine.
-   [ ] Support paper execution without exchange order permissions.
-   [ ] Support exchange sandbox/testnet where the selected exchange and
    market support it.
-   [ ] Support live execution only through an explicit configuration
    gate.
-   [ ] Persist signals, decisions, orders, fills, balances, positions,
    and operational events.
-   [ ] Reconcile local state with exchange state at startup and
    periodically.
-   [ ] Recover safely from process restarts, timeouts, partial fills,
    and transient exchange failures.
-   [ ] Expose health and read-only operational APIs.
-   [ ] Provide structured logs, metrics, and actionable alerts.

### 1.2 Non-goals for the first release

-   No leverage, margin, derivatives, short selling, or borrowing.
-   No withdrawals or transfers.
-   No unrestricted LLM/agent authority to submit orders or change risk
    limits.
-   No high-frequency trading or latency-sensitive market making.
-   No multi-exchange smart order routing.
-   No Kubernetes requirement.
-   No claim or promise of profitability. Strategy performance is
    uncertain and must be evaluated empirically.

### 1.3 Definition of done

The first production-candidate release is complete only when:

-   [ ] Paper trading runs continuously for a defined soak-test period
    without data loss, duplicate simulated orders, or unhandled crashes.
-   [ ] Restart recovery and reconciliation tests pass, including
    ambiguous order submission outcomes.
-   [ ] Risk limits are enforced in unit, integration, and
    failure-injection tests.
-   [ ] Live mode is impossible to activate accidentally.
-   [ ] All order lifecycle transitions are persisted and auditable.
-   [ ] Monitoring detects stale data, unhealthy dependencies, and
    reconciliation discrepancies.
-   [ ] Runbooks document startup, shutdown, recovery, key rotation, and
    emergency stop.
-   [ ] A human has reviewed the risk policy, exchange permissions, and
    rollout checklist before any live deployment.

------------------------------------------------------------------------

## 2. Design principles

1.  **Strategy is not execution.** Strategies emit signals; they never
    call CCXT.
2.  **Risk is an independent gate.** No strategy can bypass the risk
    manager.
3.  **Persist before side effects.** Record intent and state transitions
    before submitting external orders.
4.  **Assume requests can have ambiguous outcomes.** A timeout does not
    prove an order failed.
5.  **Exchange is authoritative for exchange-side facts.** Reconcile
    orders, fills, balances, and positions against the venue.
6.  **Fail closed.** Missing/stale data, unavailable risk state,
    database errors, or uncertain reconciliation block new orders.
7.  **Idempotency is explicit.** Every intent has a stable internal
    identifier and, where supported, a deterministic client order ID.
8.  **Use decimal arithmetic for money and quantities.** Avoid binary
    floating-point for financial calculations.
9.  **Keep the first deployment simple.** Start with one process and
    PostgreSQL; introduce Redis only when there is a demonstrated need.
10. **No silent recovery.** Material discrepancies and safety-triggered
    halts must be visible and recorded.

------------------------------------------------------------------------

## 3. Target architecture

### 3.1 Logical components

``` text
                     Read-only API / Dashboard
                               |
                               v
+----------------------------------------------------------------+
|                       Application Runtime                      |
|                                                                |
|  Market Data --> Bar Builder --> Strategy --> Signal Store     |
|                                      |                         |
|                                      v                         |
|                                Position Sizer                  |
|                                      | sized intent            |
|                                      v                         |
|                                Risk Manager                    |
|                                      | approved intent         |
|                                      v                         |
|                                Order Manager                   |
|                                      |                         |
|                                      v                         |
|                               CCXT Adapter                     |
|                                      |                         |
+--------------------------------------+-------------------------+
                                       |
                                       v
                                  Exchange API

             PostgreSQL: durable state, event log, audit trail
             Supervisor: health checks, reconciliation, kill switch
             Metrics/logging/alerts: operational visibility
```

### 3.2 Deployment topology for MVP

Run a single application container with internal asyncio tasks and a
PostgreSQL service using Docker Compose. The app may expose FastAPI for
read-only status and operator controls. Do not split into microservices
until scaling, isolation, or independent deployment requirements justify
it.

Suggested runtime tasks:

-   Market data polling/stream ingestion.
-   Candle finalization and gap detection.
-   Strategy evaluation.
-   Order submission and order-status polling.
-   Reconciliation.
-   Health supervision.
-   Metrics/API server.

Use bounded queues and explicit task cancellation/shutdown. A task
failure in a safety-critical subsystem must transition the system to a
safe state rather than being swallowed.

### 3.2.1 Concurrency model

The application is **async from day 1**. All I/O (exchange, database,
HTTP, notifications) uses async clients awaited on a single event loop:
`ccxt.async_support` for the exchange, SQLAlchemy's async engine with
`psycopg` (v3) for PostgreSQL, and `httpx.AsyncClient` for webhooks.
Never call blocking I/O from the event loop; CPU-bound work (e.g.
backtest metrics) runs off-loop or in a separate process. Use
`asyncio.TaskGroup` for structured task lifetimes and `asyncio.timeout`
for bounded waits. Domain, strategy-indicator, sizing, and risk-rule
logic stays synchronous and pure; only orchestration and adapters are
`async`.

### 3.3 Module boundaries

-   `domain`: immutable entities, enums, value objects, and domain
    events; standard library only (no CCXT, SQLAlchemy, pydantic, or
    FastAPI imports).
-   `application`: use cases and orchestration; coordinates domain
    services and ports.
-   `ports`: protocols the application depends on. The venue is split
    into three ports so paper mode can combine real market data with
    simulated execution:
    -   `MarketDataPort` --- markets, OHLCV, ticker/order book (read-only,
        no credentials required).
    -   `ExecutionPort` --- submit/cancel/fetch orders and trades.
    -   `AccountPort` --- balances and account-level state.
-   `exchange`: CCXT implementation of the venue ports and
    venue-specific capability handling.
-   `market`: ingestion, normalization, candle storage, freshness, and
    gap checks.
-   `strategy`: strategy interface and implementations.
-   `sizing`: converts a signal into a sized order intent from
    portfolio state, sizing policy, and market constraints (precision,
    minimums). It proposes; it never approves.
-   `risk`: deterministic, independent gate that approves or rejects a
    sized intent; exposure calculation and kill switch. Risk never
    resizes an intent --- it rejects with a reason code.
-   `execution`: order lifecycle, submission, retries, fill tracking,
    and reconciliation.
-   `portfolio`: balances, positions, realized/unrealized P&L, and
    snapshots.
-   `persistence`: SQLAlchemy tables, repositories, migrations,
    transaction boundaries, single-instance lock.
-   `api`: FastAPI read-only endpoints and explicitly authorized
    operator actions.
-   `monitoring`: structured logs, metrics, health status, and
    notifications.
-   `backtest`: historical simulation, costs, and reporting.
-   `paper`: simulated `ExecutionPort` and `AccountPort` driven by real
    market data; uses the shared fill model in `simulation`.
-   `simulation`: fill/fee/slippage models shared by `paper` and
    `backtest` so the two cannot drift apart.
-   `config`: validated configuration and mode-specific policy.

------------------------------------------------------------------------

## 4. Repository layout

``` text
arbitron/
├── pyproject.toml
├── uv.lock
├── Dockerfile                  # non-root runtime image (pinned bases)
├── .dockerignore
├── README.md
├── plan.md
├── .env.example
├── .gitignore
├── .github/
│   └── workflows/
│       └── ci.yml
├── alembic.ini                 # dev tooling; migrations ship in the package
├── configs/
│   ├── example.paper.toml
│   └── example.live.toml
├── src/
│   └── arbitron/
│       ├── __init__.py
│       ├── __main__.py
│       ├── cli.py
│       ├── config/
│       │   ├── settings.py
│       │   └── validation.py
│       ├── clocks.py               # SystemClock, ManualClock
│       ├── domain/
│       │   ├── enums.py
│       │   ├── errors.py
│       │   ├── ids.py              # deterministic IDs, client order IDs
│       │   ├── money.py
│       │   ├── market.py
│       │   ├── signals.py
│       │   ├── orders.py           # intents, orders, central state machine
│       │   ├── fills.py
│       │   ├── positions.py
│       │   ├── portfolio.py
│       │   ├── risk.py             # risk decisions, reason codes, kill switch
│       │   ├── strategies.py       # config snapshots, evaluation cursors
│       │   ├── snapshot.py         # TradingSnapshot read by sizing and risk
│       │   ├── experiments.py      # ExperimentRecord (backtest provenance)
│       │   └── events.py
│       ├── ports/
│       │   ├── errors.py           # venue error taxonomy (§8.1)
│       │   ├── market_data.py
│       │   ├── execution.py
│       │   ├── account.py
│       │   ├── repositories.py
│       │   ├── clock.py
│       │   └── notifier.py
│       ├── exchange/
│       │   ├── ccxt_adapter.py     # MarketDataPort over ccxt.async_support
│       │   ├── capabilities.py     # `has` flags, supported venues
│       │   ├── mapping.py          # CCXT payload -> domain (exact Decimals)
│       │   ├── retry.py            # bounded backoff for safe reads only
│       │   └── errors.py           # CCXT -> venue error taxonomy
│       ├── market/
│       │   ├── collector.py        # closed-candle ingestion
│       │   ├── candles.py          # alignment, series checks, gaps
│       │   ├── freshness.py        # data-quality gate (§9.2)
│       │   ├── service.py          # sync cycle / follow loop
│       │   └── backfill.py         # paged historical candles + market rules
│       ├── strategy/               # stdlib + domain imports only (tested)
│       │   ├── base.py             # Strategy protocol, context, proposals
│       │   ├── evaluation.py       # shared live/replay evaluation, replay()
│       │   ├── registry.py         # config -> strategy, version check
│       │   └── implementations/
│       │       └── sma_cross.py
│       ├── sizing/                 # stdlib + domain imports only (tested)
│       │   └── sizer.py            # fixed_quote_notional_v1, IOC limits
│       ├── risk/                   # stdlib + domain imports only (tested)
│       │   ├── manager.py          # RiskPolicy, decide(), loss breaches
│       │   └── exposure.py         # valuation, worst-case exposure
│       ├── execution/
│       │   ├── order_manager.py    # signal -> intent -> risk -> submit -> fills
│       │   ├── freshness.py        # which signals may be traded
│       │   └── reconciler.py       # venue vs local records (docs/failure-matrix.md)
│       ├── portfolio/              # stdlib + domain imports only (tested)
│       │   └── accounting.py       # weighted average cost from fills
│       ├── simulation/             # stdlib + domain imports only (tested)
│       │   ├── fill_model.py       # top of book + slippage, fees in received asset
│       │   ├── settlement.py       # balance deltas per fill (paper + backtest)
│       │   └── quotes.py           # synthetic bid/ask from a candle price
│       ├── paper/
│       │   └── broker.py           # simulated venue (ExecutionPort + AccountPort)
│       ├── backtest/               # stdlib + domain imports only (tested)
│       │   ├── engine.py           # candle loop, next-open fills, risk + kill switch
│       │   ├── metrics.py          # return, drawdown, volatility, costs, benchmark
│       │   └── report.py           # JSON / Markdown reports, sweep table
│       ├── persistence/
│       │   ├── db.py               # engine/sessions, schema verification
│       │   ├── lock.py             # advisory-lock single-instance guard
│       │   ├── mapping.py          # row <-> domain conversions
│       │   ├── tables.py
│       │   ├── unit_of_work.py
│       │   ├── migrations/         # Alembic env + versions (packaged)
│       │   └── repositories/
│       ├── application/
│       │   ├── supervisor.py       # task lifecycle, restart policy, safety trip
│       │   ├── paper_runtime.py    # supervised paper process + readiness
│       │   ├── strategy_runner.py  # live evaluation, cursor, persistence
│       │   ├── pipeline.py         # market cycle -> quality gate -> strategy
│       │   ├── policies.py         # config -> RiskPolicy / SizingPolicy
│       │   ├── risk_gate.py        # persist decision + order transition
│       │   ├── kill_switch.py      # operator kill-switch service
│       │   ├── snapshot_builder.py # TradingSnapshot + equity history
│       │   ├── paper_session.py    # startup recovery + full paper loop per candle
│       │   ├── order_resolution.py # audited operator resolution (zero-fill only)
│       │   ├── backtesting.py      # load from DB, run, record experiments, sweeps
│       │   ├── operations.py       # operator actions over the API (audited)
│       ├── api/
│       │   ├── app.py              # routes: reads + two audited actions
│       │   ├── auth.py             # bearer token, constant-time compare
│       │   └── views.py            # domain -> JSON (exact decimal strings)
│       └── monitoring/
│           ├── logging.py          # JSON logs + redaction
│           ├── metrics.py          # Prometheus (dedicated registry)
│           ├── health.py           # heartbeats, readiness checks
│           ├── alerts.py           # Telegram notifier, dispatcher, playbook
│           └── http.py             # uvicorn serving, bind-failure handling
├── typings/ccxt/               # minimal type stubs for the CCXT surface used
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── contract/               # real CCXT parsing over recorded venue responses
│   ├── failure/
│   ├── live/                   # opt-in calls to the real venue (never in CI)
│   └── fixtures/
├── scripts/
│   ├── seed_paper_account.py
│   └── verify_config.py
├── docs/
│   ├── failure-matrix.md       # operator-approved failure behavior (Phase 7)
│   └── deployment.md           # deploy, API, backup/restore, rotation, incidents
└── deploy/
    ├── compose.dev.yaml        # local PostgreSQL for development
    ├── compose.yaml            # app, postgres, migrate, backup; monitoring profile
    ├── setup.sh                # random secrets + config, never overwrites
    ├── arbitron.example.toml   # deployment config template (paper)
    ├── postgres/init-roles.sh  # owner vs least-privilege app role
    ├── backup/                 # backup.sh loop, restore.sh verify|replace
    └── monitoring/             # prometheus.yml, Grafana provisioning + dashboard
```

The coding agent may adjust filenames when needed, but must preserve
these boundaries and explain material deviations.

------------------------------------------------------------------------

## 5. Technology and dependency policy

Use `uv` for environment and dependency management. Prefer a `src/`
layout and typed Python.

Core dependencies:

-   `ccxt` --- exchange REST API, used only via `ccxt.async_support`.
-   `pydantic-settings` --- configuration.
-   `sqlalchemy[asyncio]` 2.x --- typed declarative table mappings and
    async engine/sessions. (SQLModel was the original choice; it was
    replaced because the domain already uses its own dataclasses, so
    SQLModel's shared model/table classes add a second model layer
    without benefit, while SQLAlchemy's typed mappings work directly
    with pyright strict and PostgreSQL features such as `ON CONFLICT`.)
-   `psycopg[binary]` (v3) --- async PostgreSQL driver
    (`postgresql+psycopg://`).
-   `alembic` --- schema migrations (async `env.py`).
-   `fastapi` and `uvicorn` --- operational API.
-   `httpx` --- async alert/webhook clients (`AsyncClient`).
-   `structlog` or standard-library structured logging --- consistent
    logs.
-   `prometheus-client` --- metrics.
-   `pandas` only where it materially simplifies research/backtesting;
    avoid making it a core domain dependency.
-   `pytest`, `pytest-asyncio`, `respx`/mocking utilities, and coverage
    tooling for tests.
-   `ruff` --- lint/format.
-   `pyright` in strict mode --- static type checking. Re-evaluate
    Astral's `ty` once it is stable enough to replace pyright in CI.

Use `Decimal` for prices, quantities, fees, and balances in
domain/accounting logic. Convert CCXT numeric values at the adapter
boundary using string-based conversion where possible, and document
precision/rounding behavior.

Store Decimal values in PostgreSQL as `NUMERIC(38, 18)` for amounts,
prices, fees, and balances unless a column documents a different
precision. Never store financial values as `float`/`double precision`.

Pin compatible dependency ranges and commit `uv.lock`. Never log API
keys, secrets, authentication headers, or full sensitive request
payloads.

------------------------------------------------------------------------

## 6. Domain model

Keep domain types independent of persistence and exchange libraries. Use
dataclasses or Pydantic models for value objects, with validation at
construction.

### 6.1 Core entities

**Market** - `exchange_id` - `symbol` (unified symbol,
e.g. `BTC/USDT`) - `base` - `quote` - `market_type` (spot initially) -
`amount_step`, `price_step`, `min_amount`, `min_cost` - `active` -
capability metadata

**Candle** - `exchange_id`, `symbol`, `timeframe` - `open_time`,
`close_time` - OHLCV as Decimal - `is_closed` - source/ingestion
timestamp

Unique identity: `(exchange_id, symbol, timeframe, open_time)`.

**Signal** - `id` - `strategy_id`, `strategy_version` - `exchange_id`,
`symbol` - `side` - `created_at`, `source_candle_time` - `reason_code`,
structured explanation - optional strategy metadata (not trusted for
risk) - deterministic deduplication key

A signal carries direction and rationale only, never a quantity.

**OrderIntent** - `id` (UUID/ULID) - `signal_id` - `strategy_id` -
`exchange_id`, `symbol` - `side`, `order_type` - requested amount
(produced by the sizer, already rounded to exchange precision) and
optional limit price - sizing policy and inputs used - `time_in_force` if supported - creation
timestamp - risk decision reference - client order ID (when supported) -
state

**ExchangeOrder** - internal intent ID - exchange order ID - client
order ID - exchange status - amount, filled, remaining, average price -
fee data - raw response reference or sanitized metadata - timestamps

**Fill** - exchange trade ID (when available) - order ID - symbol,
side - amount, price, fee, fee currency - timestamp - deduplication key

**Position** - exchange, symbol - signed quantity (spot long-only
initially) - average cost - realized P&L - updated timestamp -
source/reconciliation status

**RiskDecision** - intent/signal reference - `approved` boolean - reason
codes - evaluated limits and observed values - policy version -
timestamp

**SystemEvent** - event type, severity, component - timestamp -
correlation ID - sanitized structured payload

### 6.2 State machines

Order statuses are normalized to an internal enum. Happy path:

`CREATED → RISK_APPROVED → SUBMITTING → OPEN → PARTIALLY_FILLED → FILLED`

**Non-terminal states**

| State | Meaning |
|---|---|
| `CREATED` | Sized intent persisted; risk not yet evaluated. |
| `RISK_APPROVED` | Risk decision persisted and approved. |
| `SUBMITTING` | Persisted immediately before the external submit call. |
| `OPEN` | Exchange acknowledged; nothing filled. |
| `PARTIALLY_FILLED` | Exchange acknowledged; some quantity filled. |
| `CANCEL_REQUESTED` | Cancel sent; fills may still arrive. |
| `SUBMISSION_UNKNOWN` | Submit outcome ambiguous (timeout, crash, lost response). Blocks new orders for the market until resolved. |
| `RECONCILIATION_REQUIRED` | Local and exchange state disagree, or the outcome could not be established. Leaves only via the reconciler or audited operator resolution. |

**Terminal states**

| State | Meaning |
|---|---|
| `RISK_REJECTED` | Risk gate rejected the intent; never sent. |
| `EXCHANGE_REJECTED` | Exchange explicitly rejected the order. |
| `NOT_PLACED` | Reconciliation proved the exchange never received a `SUBMITTING`/`SUBMISSION_UNKNOWN` order. |
| `FILLED` | Fully filled. |
| `CANCELED` | Canceled with zero filled quantity. |
| `EXPIRED` | Expired (e.g. time-in-force) with zero filled quantity. |
| `PARTIALLY_FILLED_CLOSED` | Canceled or expired after a partial fill; the closing reason is recorded in `order_events`. |

**Allowed transitions**

-   `CREATED → RISK_APPROVED | RISK_REJECTED`
-   `RISK_APPROVED → SUBMITTING`
-   `SUBMITTING → OPEN | PARTIALLY_FILLED | FILLED | CANCELED | EXPIRED | PARTIALLY_FILLED_CLOSED | EXCHANGE_REJECTED | SUBMISSION_UNKNOWN`
    (an immediate-or-cancel order can close in the submit response)
-   `OPEN → PARTIALLY_FILLED | FILLED | CANCEL_REQUESTED | CANCELED | EXPIRED | PARTIALLY_FILLED_CLOSED`
    (polling may skip the intermediate partially-filled observation)
-   `PARTIALLY_FILLED → PARTIALLY_FILLED | FILLED | CANCEL_REQUESTED | PARTIALLY_FILLED_CLOSED`
-   `CANCEL_REQUESTED → CANCEL_REQUESTED | OPEN | PARTIALLY_FILLED | FILLED | CANCELED | EXPIRED | PARTIALLY_FILLED_CLOSED`
    (self-transition records fills that race the cancel; `OPEN`/`PARTIALLY_FILLED`
    when the venue refuses the cancel and the order is still live)
-   `SUBMISSION_UNKNOWN → OPEN | PARTIALLY_FILLED | FILLED | CANCELED | EXPIRED | PARTIALLY_FILLED_CLOSED | EXCHANGE_REJECTED | NOT_PLACED | RECONCILIATION_REQUIRED`
-   Any other non-terminal state → `RECONCILIATION_REQUIRED`
-   `RECONCILIATION_REQUIRED → OPEN | PARTIALLY_FILLED | FILLED | CANCELED | EXPIRED | PARTIALLY_FILLED_CLOSED | EXCHANGE_REJECTED | NOT_PLACED`,
    only as established by verified exchange facts (recorded with
    provenance); never back to a pre-submission state or `SUBMITTING`.

Filled quantity never decreases across a transition. Status/fill
invariants: pre-submission states, `OPEN`, `CANCELED`, `EXPIRED`,
`EXCHANGE_REJECTED`, and `NOT_PLACED` have zero filled quantity;
`PARTIALLY_FILLED` and `PARTIALLY_FILLED_CLOSED` have
`0 < filled < amount`; `FILLED` has `filled == amount`.

Terminal states are final; a fill observed for an order in a terminal
state is a reconciliation discrepancy, not a silent update. A
`NOT_PLACED` order is never resubmitted; any retry is a new intent with
a new client order ID.

Define allowed transitions centrally. Never allow arbitrary status
updates from strategy code. Preserve exchange raw status separately
because venue semantics differ.

------------------------------------------------------------------------

## 7. Persistence and database design

Use PostgreSQL as the durable source for the bot's own decisions and
lifecycle. Use Alembic migrations; do not rely on `create_all()` in
production. Each table is introduced by the migration of the phase that
first uses it (Phase 2 created candles, signals, orders, order_events,
fills, risk_decisions, kill_switch, kill_switch_events, and
system_events). Append-only tables are enforced by a trigger that
rejects UPDATE and DELETE; enumerated columns are text with CHECK
constraints.

Minimum tables:

### 7.1 Configuration and runs

**`bot_instances`** - `id`, `name`, `mode`, `environment` -
`started_at`, `stopped_at`, `version` - `status`, `config_hash`

**`strategy_configs`** - `config_hash` (PK), `strategy_id`, `version`,
`parameters`, `created_at` - immutable versioned config snapshots
(append-only). *Implemented in Phase 4.* `enabled` stays in the TOML
config: it is operational state, not part of the snapshot's identity.

**`strategy_cursors`** - `config_hash`, exchange, symbol, timeframe (PK) -
`last_open_time`, `updated_at`. *Implemented in Phase 4.*

**`strategy_runs`** - `id`, `bot_instance_id`, `strategy_id`,
`started_at`, `ended_at` - `status`, `last_processed_event`

### 7.2 Market data

**`markets`** - exchange/symbol identity and trading constraints -
unique `(exchange_id, symbol)`

**`candles`** - exchange, symbol, timeframe, open time - OHLCV, closed
flag, ingestion time - unique
`(exchange_id, symbol, timeframe, open_time)` - index for chronological
retrieval

### 7.3 Trading records

**`signals`** - signal identity, strategy/version, `config_hash` (FK),
market, direction, reason, timestamps - unique dedupe key; append-only

**`risk_decisions`** - signal/intent reference, approved, reason codes,
observed metrics, policy version

**`orders`** - internal ID, client ID, exchange order ID - market, side,
type, requested amount/price - filled/remaining, status, timestamps -
unique constraints on non-null exchange/client identifiers scoped by
exchange

**`order_events`** - append-only lifecycle events - order ID, event
type, timestamp, correlation ID, sanitized payload

**`fills`** - order ID, exchange trade ID, amount, price, fee, fee
currency, timestamp - unique dedupe key; unique
`(exchange_id, trade_id)` when available

### 7.4 Portfolio and operations

**`balances`** - exchange, asset, free/used/total, observed time -
snapshots, not only latest values

**`positions`** - exchange, symbol, quantity, average cost, realized
P&L, update time - specify accounting method (e.g. weighted average) and
test it

**`portfolio_snapshots`** - timestamp, total equity estimate, quote
currency, component breakdown

**`risk_events`** - trigger, severity, action, policy version, observed
value, timestamp

**`system_events`** - component, severity, event type, payload,
correlation ID, timestamp

**`heartbeats`** - component, instance, last heartbeat, status, metadata

Use database transactions around internal state transitions. Use
append-only events for auditability; updates to current-state tables
should be derived/consistent with event records. Define retention and
backup policy before live use.

**Single-instance lock.** At startup the process acquires a
session-level PostgreSQL advisory lock (`pg_try_advisory_lock`) keyed
by a stable hash of `(exchange_id, account identifier, mode)`, held on
a dedicated connection for the life of the process. If the lock is not
acquired, startup fails. If the lock connection drops, the process
treats it as a critical failure: block new orders, trip the safety
state, and attempt orderly shutdown --- another instance may now hold
the lock.

------------------------------------------------------------------------

## 8. Exchange adapter (CCXT)

Implement a narrow port interface. The rest of the application must not
import CCXT.

Example responsibilities:

-   Initialize exchange with explicit ID and validated settings.
-   `load_markets()`, validate symbol, fetch market constraints.
-   Fetch OHLCV/ticker/order book only as required.
-   Fetch balances, open orders, individual orders, and recent
    trades/fills where supported.
-   Submit/cancel orders.
-   Normalize CCXT exceptions into internal categories.
-   Enforce rate limits and bounded timeouts.
-   Expose exchange capabilities (`has`) and venue-specific limitations.
-   Sanitize and store relevant response metadata.
-   Close resources on shutdown.

### 8.1 Error taxonomy

Map errors into at least:

-   `NetworkTransient`
-   `RequestTimeout`
-   `RateLimited`
-   `AuthenticationFailure`
-   `InsufficientFunds`
-   `InvalidOrder`
-   `MarketUnavailable`
-   `ExchangeUnavailable`
-   `UnknownExchangeError`

Do not retry non-retryable errors. Use bounded exponential backoff with
jitter for transient reads. CCXT `DuplicateOrderId` maps to
`UnknownExchangeError`, not `InvalidOrder`: it means an order with that
client ID may already exist, so the outcome must be reconciled. For order submissions, **never blindly retry
after timeout**; mark submission outcome unknown and reconcile first.

### 8.2 Precision and constraints

Before submitting an order:

-   Verify market is active and symbol is valid.
-   Apply exchange precision formatting using CCXT market methods where
    appropriate.
-   Check minimum amount and minimum notional/cost.
-   Validate quote/base asset and order type.
-   Reject if required capability is missing.
-   Never assume custom parameters are portable between exchanges.
-   Keep venue-specific parameters behind the adapter and covered by
    contract tests.

### 8.3 Sandbox and credentials

Sandbox/testnet must be configured explicitly and verified for the
selected exchange. Never assume all exchange methods or symbols work
identically in sandbox. Use separate credentials and account
configuration for paper, sandbox, and live environments.

Live credentials must have trading-only permissions where possible, no
withdrawal permission, IP restrictions where supported, and dedicated
accounts/subaccounts where feasible.

------------------------------------------------------------------------

## 9. Market-data subsystem

### 9.1 Ingestion

Start with REST polling of closed OHLCV candles for a small allowlist of
spot symbols and one timeframe. Add WebSocket streaming only when needed
and only after reconnection/gap recovery behavior is tested.

Responsibilities:

-   Poll according to timeframe and exchange limits.
-   Normalize timestamps to UTC.
-   Distinguish incomplete/current candles from finalized candles.
-   Persist candles idempotently.
-   Detect gaps, duplicates, out-of-order bars, and stale updates.
-   Backfill gaps with bounded requests.
-   Track last successful update and source latency.
-   Do not evaluate a candle-based strategy more than once per finalized
    candle unless explicitly designed otherwise.

### 9.2 Data-quality gates

Block new orders when:

-   Most recent finalized candle is older than configured freshness
    threshold.
-   Required lookback history is incomplete.
-   Timestamp order is invalid.
-   OHLC values violate basic invariants.
-   Exchange market metadata is unavailable or stale.
-   System clock health is outside configured tolerance.

Record a data-quality event and alert according to severity.

### 9.3 Rate limiting

Use CCXT's rate limiting and avoid unbounded concurrent calls.
Centralize request concurrency and use per-exchange limits. Instrument
request count, latency, error rate, and rate-limit responses.

------------------------------------------------------------------------

## 10. Strategy subsystem

### 10.1 Interface

Implemented protocol (`src/arbitron/strategy/base.py`):

``` python
class Strategy(Protocol):
    strategy_id: str
    version: str          # must equal the config's version (enforced by the registry)
    parameters: Mapping[str, ParameterValue]
    lookback: int         # closed candles per evaluation, including the current one

    def evaluate(self, context: StrategyContext) -> Sequence[SignalProposal]: ...
```

`evaluate` is deliberately **synchronous**, not `async` as originally
sketched: a sync function cannot await I/O, so "no network or database
access from strategies" is enforced by construction (consistent with
§3.2.1). Strategies return direction-only `SignalProposal`s (side, reason,
explanation, metadata), at most one per candle; the engine stamps identity
(strategy, version, config hash, market, candle time) onto the `Signal`, so
a strategy cannot forge it. Any Decimal placed in signal text must be
formatted with `domain.money.canonical_str` so the text does not depend on
the Decimal's exponent (values read from `NUMERIC(38,18)` differ in
representation from equal in-memory values).

`StrategyContext` should provide only required, immutable inputs: recent
candles/features, current strategy-owned state, and a read-only
portfolio view. It must not expose CCXT, database sessions, arbitrary
network clients, or order submission methods.

### 10.2 Determinism and replay

-   Same input history + same config/version should produce the same
    signals. Verified by a test that runs the live runner candle by
    candle and compares its persisted signals with `replay` over the same
    candles.
-   Persist strategy version and parameter snapshot for each run: the
    immutable `strategy_configs` row keyed by `config_hash` (strategy +
    version + parameters). The config hash is part of every signal's
    identity, so changing a parameter yields a distinct signal stream.
-   Make event processing idempotent using a stable key:
    strategy/version/config hash/market/timeframe/candle timestamp. A
    per-market `strategy_cursors` row records the last evaluated candle and
    advances in the same transaction as the signals.
-   On first run (no cursor) the live runner evaluates only the latest
    closed candle; history is never back-filled into persisted signals.
    After downtime it evaluates every missed candle in order, up to a
    bounded catch-up limit (excess is skipped and recorded). Signals from
    catch-up describe past candles; the order pipeline must refuse to act
    on stale signals (Phase 6).
-   Evaluation is refused unless the market-data quality gate for that
    symbol is `ok`, using the strategy's own lookback as the requirement.
-   Support replay from stored candles for debugging and backtesting.
-   **Strategy state is recomputed, not checkpointed, for the MVP.** On
    startup or replay, a strategy rebuilds its state from stored
    finalized candles over its declared lookback window. Strategies
    must declare that lookback; the engine refuses to evaluate until
    the full window is available. Checkpointing may be added later
    only for strategies whose state cannot be bounded by a lookback
    window.

### 10.3 Initial example strategy

Implement one simple illustrative strategy (e.g. moving-average
crossover) strictly as a software integration fixture, not as a claim of
profitability. Include warm-up periods, no-lookahead behavior, and
deterministic signal generation. Keep it disabled by default in live
mode.

------------------------------------------------------------------------

## 11. Risk management

Risk management is a deterministic, separately tested service. It
receives a sized intent and a current portfolio/exchange health
snapshot and returns an immutable decision with reason codes. It
approves or rejects; it never changes the intent's quantity or price.

### 11.1 Required controls

Configurable limits should include:

-   Maximum notional per order.
-   Maximum notional per asset and total portfolio exposure.
-   Maximum number of open orders.
-   Maximum daily realized + unrealized loss (define valuation source).
    A trading "day" is the UTC calendar day (00:00--24:00 UTC) unless
    configured otherwise; the boundary is part of the risk policy
    version.
-   Maximum peak-to-trough drawdown.
-   Maximum order frequency / churn.
-   Minimum available balance reserve.
-   Maximum spread/slippage estimate.
-   Market-data maximum age.
-   Maximum exchange/API error rate.
-   Trading schedule/allowed market list.
-   Global trading-enabled flag and kill-switch state.

Choose conservative initial values for paper mode; live values require
explicit operator configuration and review. Do not invent a universally
safe risk percentage.

### 11.1.1 Operator decisions (2026-10-12)

-   **Sizing:** fixed quote amount per entry (`[sizing] entry_notional`),
    policy `fixed_quote_notional_v1`.
-   **Positions:** BUY only when flat (no adding); SELL closes the whole
    position. Holdings below the venue minimums are dust and count as flat.
-   **Pricing:** marketable limit orders, time-in-force IOC: BUY at best
    ask + `max_slippage_pct` (rounded down to the tick), SELL at best bid −
    `max_slippage_pct` (rounded up). Market orders stay disabled.
-   **Valuation source:** each base asset is marked at its symbol's last
    closed candle close; only the quote currency and configured base assets
    are valued (other holdings are reported, not valued). Ticker bid/ask is
    used at order time for pricing and the spread/band checks.
-   **Exits are exempt from risk-increasing limits.** Per-order notional,
    exposure, open-order count, order frequency, daily loss, drawdown,
    balance/reserve, and the flat-only rule apply to BUYs. Fail-closed
    conditions (kill switch, trading disabled, readiness, reconciliation,
    exchange health, market data, ticker, precision, minimums, price band)
    apply to every order. Blocking an exit when limits are breached would
    increase risk.
-   **Boundaries:** daily loss and drawdown block entries once they *reach*
    the limit (`>=`); notional/exposure limits allow exactly the limit.

### 11.2 Position sizing (sizer, upstream of risk)

Sizing lives in the `sizing` module, not in the risk engine, so the risk
gate stays independent of the component that proposes quantities.

The sizer uses a documented formula based on account equity, risk
budget, entry price, and stop distance if the strategy has a defined
stop. For spot-only MVP it never proposes negative holdings and caps
sells at the available position after accounting for open sell orders.
All quantities are rounded down to exchange precision; if the rounded
quantity falls below the market minimum amount/cost, the sizer emits no
intent and records a reason code.

The risk engine then **independently re-verifies** these properties
(no negative holdings, sells within available position, precision and
minimums satisfied) and rejects violations rather than correcting them.
Risk checks must use worst-case open-order exposure, not only filled
positions.

### 11.3 Kill switch

Implement a durable kill-switch state with: - `ARMED` (normal: trading
permitted, switch ready to trip) / `TRIPPED` (new orders blocked) /
`RECOVERY_REVIEW` (blocked pending operator acknowledgment) states. - Trigger, timestamp, observed values, and
operator acknowledgment. - Immediate block on new order intents. -
Explicit policy for existing open orders (cancel, retain, or manual
review); default to canceling unfilled entry orders when safely
supported, then reconcile. - Never automatically resume live trading
after a critical safety trip without a defined, auditable recovery
procedure.

Provide both configuration-based and operator-triggered kill switch. The
emergency endpoint must be authenticated and audited. Avoid making a web
dashboard the only way to stop trading.

Implemented (Phase 5): first initialization of the kill switch is
`RECOVERY_REVIEW`, so trading begins only after a named operator runs
`arbitron killswitch arm --operator NAME --ack "..."`. `trip` works from
any state. Every change writes a history row and a system event.
`loss_limit_breaches()` reports daily-loss/drawdown breaches for the
automatic trip, which the Phase 6 order pipeline wires in.

### 11.3.1 Database enforcement

A trigger on `orders` refuses `risk_approved` unless an approved
`risk_decisions` row exists for that intent (and `risk_rejected` without a
rejecting one), and refuses inserting orders in any state but `created`.
No code path, including direct SQL, can approve an order without a
persisted risk decision.

### 11.4 Fail-closed conditions

No new orders if: - Risk service errors or cannot obtain required
portfolio state. - Database is unavailable. - Reconciliation is
unresolved. - Exchange account state differs materially from local
state. - Market data is stale or invalid. - Kill switch is tripped. -
Config validation fails. - System is in startup/recovery state.

------------------------------------------------------------------------

## 12. Order management and execution

### 12.1 Intent lifecycle

1.  Persist signal.
2.  Size the signal into an order intent (sizer) with deterministic
    dedupe key; persist it as `CREATED`.
3.  Evaluate risk and persist the decision.
4.  If rejected, transition to `RISK_REJECTED`, record reasons, and stop.
5.  If approved, transition to `RISK_APPROVED`, then persist
    `SUBMITTING` before the external call.
6.  Submit through adapter with client order ID if supported.
7.  Persist exchange response and transition state.
8.  Track order updates and fills.
9.  Reconcile periodically and after ambiguous outcomes.
10. Update portfolio from deduplicated fills and verified balances.

### 12.2 Idempotency

-   Internal intent ID is always required.
-   Generate a deterministic client order ID within venue
    length/character limits when supported.
-   Store a unique dedupe key for strategy event → intent.
-   On timeout after submission, set `SUBMISSION_UNKNOWN`.
-   Search by client ID/order history/open orders/trades using supported
    endpoints before considering another submission.
-   If outcome cannot be established, halt new trading for the affected
    market/account and raise an alert.
-   Document that true exactly-once execution cannot be guaranteed
    across an external exchange API; implement effectively-once behavior
    through deduplication and reconciliation.

### 12.3 Partial fills and cancellations

-   Treat fills as independent immutable facts.
-   Deduplicate trades by exchange trade ID where available.
-   Maintain cumulative filled quantity and remaining quantity.
-   Handle cancel requests that race with fills.
-   Do not assume cancel acknowledgment means zero additional fills.
-   Re-fetch order/trades after cancel and reconcile final state.
-   Avoid automatically replacing a partially filled order unless
    strategy and risk policy explicitly define replacement behavior.

### 12.4 Order types

MVP: market and limit only, with spot-only support. Prefer limit orders
if the strategy specifies price constraints; market orders must have an
explicit slippage/spread guard and max-notional bound. Stop/trigger
orders are deferred until exchange-specific semantics and failure
behavior are modeled and tested.

------------------------------------------------------------------------

## 13. Portfolio and accounting

Maintain local portfolio projections from fills and exchange balance
snapshots.

-   Define the quote currency and valuation source.
-   Track fees in their actual fee currency.
-   Define average-cost accounting and realized P&L formula.
-   Separate realized P&L, unrealized P&L, fees, and estimated execution
    costs.
-   Do not treat deposits/withdrawals/transfers as trading P&L.
-   For spot-only mode, positions cannot be short.
-   Compare local balances/positions to exchange snapshots and record
    discrepancies.
-   Portfolio valuation must indicate timestamp and stale/missing price
    inputs.
-   Risk decisions must reject if required valuations are missing or
    stale.

Do not silently overwrite accounting history to match an exchange
snapshot. Record adjustments as explicit reconciliation events with
provenance.

------------------------------------------------------------------------

## 14. Reconciliation and recovery

Reconciliation is a core subsystem, not a maintenance script.

### 14.1 Startup recovery sequence

1.  Load and validate config; live mode remains disabled.
2.  Acquire the single-instance/account advisory lock (§7).
3.  Connect to database and verify migrations.
4.  Connect to exchange and load markets.
5.  Fetch balances, open orders, recent closed orders, and recent
    trades/fills as supported.
6.  Compare exchange facts with local records.
7.  Resolve known transitions and deduplicate fills.
8.  Flag unknown exchange orders, missing local orders, balance
    differences, and unsupported states.
9.  If discrepancies are material or ambiguous, enter `RECOVERY_REVIEW`
    and block new orders.
10. Enable paper operation automatically if healthy; live enablement
    requires configured policy and successful safety gates.

Implemented (Phase 7) in `PaperSession.start()`, which `paper run` calls
after loading config, taking the instance lock, and verifying the schema:
1. initialize the kill switch;
2. fund the paper account if new;
3. load market metadata;
4. mark orders found in `SUBMITTING` as `SUBMISSION_UNKNOWN`;
5. run a full reconciliation.

New orders are possible only after that run, and only while reconciliation
stays clean (the snapshot's `reconciliation_clean`). The rules, timings and
expected behavior per failure are in `docs/failure-matrix.md`, approved by
the operator on 2026-10-12.

### 14.2 Periodic reconciliation

Run on a configurable interval and after order submission/cancellation
ambiguity. Compare:

-   Open orders and statuses.
-   Recent fills/trades.
-   Balances.
-   Local positions.
-   Market metadata and symbol status.

Use overlapping time windows when fetching trades to avoid boundary
gaps, relying on dedupe keys. Respect exchange history limitations and
document any state that cannot be reconstructed.

### 14.3 Repair policy

Automatically repair only deterministic, well-understood discrepancies
(e.g. a missing fill record with a verified unique trade). Otherwise: -
block affected trading, - persist discrepancy details, - notify
operator, - require explicit resolution.

Never create a compensating trade merely to make local state match a
database target.

Implemented automatic repairs are limited to two:
- applying a verified venue state to an unknown or unresolved order;
- inserting a verified missing fill for a known order, within its
  venue-reported fill.

Discrepancies record critical events and move an armed kill switch to
`RECOVERY_REVIEW`. Operators resolve stuck orders with
`arbitron orders resolve`. That command allows zero-fill outcomes only, and
refuses if the venue has any record of the order or local fills exist.

------------------------------------------------------------------------

## 15. Supervisor, lifecycle, and health

Implement an application supervisor that owns task lifecycle and health
policy.

### 15.1 Health dimensions

-   Database connectivity and transaction latency.
-   Exchange connectivity and authenticated read health.
-   Market-data freshness and gap status.
-   Strategy loop heartbeat and processing lag.
-   Order manager heartbeat and backlog.
-   Reconciliation age/status.
-   Clock synchronization indicator if available.
-   Disk/storage pressure where operationally accessible.

### 15.1.1 Implemented (Phase 8)

`arbitron paper run --follow` runs a supervised process (see
`application/paper_runtime.py`):

| task | critical | does |
|---|---|---|
| `trading` | yes | one session cycle after every candle close |
| `reconciliation` | yes | every `reconciliation_interval_seconds` |
| `lock-watch` | yes | checks the instance lock every 15 s; loss is fatal |
| `alerts` | no | dispatches critical events to the operator channel |
| `heartbeats` | no | persists task health to the `heartbeats` table |
| `http` | no | serves `/health`, `/ready` and `/metrics` on 127.0.0.1 |

- Trading cycles and reconciliation share a lock, so reconciliation never
  observes a half-recorded submission.
- `/ready` returns 200 only when startup is done, the kill switch is armed,
  reconciliation is clean, market data is ok and all critical tasks are
  beating. Otherwise it returns 503 and names the failing checks.
- Crash policy (operator decision, 2026-10-13): restart with backoff up to 3
  times in 10 minutes. One more crash trips the kill switch and stops the
  session. A fatal error, such as losing the lock, stops at once.

### 15.2 Task behavior

-   Every background task has a named lifecycle and heartbeat.
-   Use bounded retries and backoff.
-   Critical task death trips the safety state.
-   Do not restart a failed task indefinitely without escalation.
-   Graceful shutdown stops intake, blocks new orders, persists state,
    performs safe cancellation policy if configured, closes CCXT
    clients, and closes DB connections.
-   Handle SIGTERM and container stop timeout.
-   Use one active trader per exchange account unless explicit
    distributed locking and leader election are implemented.

------------------------------------------------------------------------

## 16. API and dashboard

FastAPI is for observability and controlled operations, not the trading
decision path.

Suggested read endpoints: - `GET /health` - `GET /ready` -
`GET /status` - `GET /portfolio` - `GET /positions` - `GET /orders` -
`GET /signals` - `GET /risk/events` - `GET /system/events` -
`GET /metrics` (or Prometheus exporter endpoint)

Operator actions (authenticated, audited): - Trip kill switch. -
Pause/resume paper mode. - Acknowledge recovery review. - Request
reconciliation. - Enable live mode only if all configured safety gates
pass; consider requiring a separate deployment-time secret/flag rather
than a routine API toggle.

Dashboard may be React later. First release can use API/docs and
Grafana. Never expose unauthenticated endpoints that can alter trading
state. Do not expose secrets or raw sensitive exchange payloads.

### 16.1 Implemented (Phase 10; operator decisions 2026-10-13)

-   **Authentication:** one bearer token (`ARBITRON_API_TOKEN`, at least
    32 characters, compared in constant time) guards every endpoint
    except `GET /health`, which returns only `{"status": "alive"}` for
    the container health check. `paper run --follow` refuses to start
    without a valid token. Rejected requests are counted
    (`arbitron_api_auth_failures_total`). No OpenAPI or docs pages.
-   **Reads:** `/ready`, `/metrics`, `/status`, `/portfolio`,
    `/positions`, `/orders`, `/signals`, `/risk/decisions`,
    `/system/events` (limits 1-500). Decimals are exact strings.
-   **Actions (safer-only):** `POST /kill-switch/trip` (operator +
    reason) and `POST /reconciliation` (operator). The actor is recorded
    as `api:<operator>` in the kill-switch history and system events.
    Review, arm, and order resolution remain CLI-only (host shell
    access). Pause/resume is not exposed; tripping is the stop.
-   **Exposure:** all ports bind to the host's `127.0.0.1`; operators use
    an SSH tunnel. No TLS proxy.
-   **Deployment:** `deploy/compose.yaml` (see `docs/deployment.md`):
    non-root read-only app container, pinned image digests, secrets as
    files (`ARBITRON_SECRETS_DIR`), migrations by the schema owner and
    runtime as a least-privilege role (no DDL, no TRUNCATE), daily
    verified `pg_dump` with 14-day retention and a tested
    `restore.sh verify|replace`, optional Prometheus (bearer-token
    scrape) and Grafana with a provisioned dashboard.

------------------------------------------------------------------------

## 17. Backtesting and paper trading

### 17.1 Backtest requirements

-   Use historical candles with explicit timezone and interval
    semantics.
-   Avoid lookahead bias: a candle-close strategy acts only after the
    candle is finalized.
-   Model fees, spread, slippage, latency assumptions, and order fill
    rules.
-   Handle missing candles and market outages explicitly.
-   Report return, drawdown, volatility, turnover, fees, trade count,
    exposure, and benchmark comparison.
-   Keep training/parameter selection periods separate from
    out-of-sample evaluation.
-   Include sensitivity analysis; do not select a strategy solely on one
    historical metric.
-   Persist experiment config, data range, code/strategy version, and
    outputs.

### 17.1.1 Implemented backtester (Phase 9; operator decisions 2026-10-13)

-   **Data:** `arbitron market backfill` pages closed candles from the
    venue into the same `candles` table the collector uses (same
    structural checks; stored closed candles are never rewritten) and
    stores market rules in `markets`. Backtests read only the database,
    loading one strategy lookback of warm-up before `start`.
-   **Timing:** a decision at a candle's close fills only at the very
    next candle's open (latency = one interval). If that candle is
    missing, the order expires; it never fills at a later price the
    decision did not see. No candle at or after the decision is visible
    to the strategy.
-   **Fills and costs:** a synthetic quote around the open
    (`spread_bps`, bid rounded down / ask up to the tick) feeds the
    shared `FillModel` (`slippage_bps`, IOC limit from the sizer, fees in
    the received asset) and the shared `settlement_deltas`, so paper and
    backtest settle identically. Defaults in `[backtest]`: 1,000 USDT,
    1 bps spread, 5 bps slippage, 0.1% fee.
-   **Risk:** the full sizing and risk policy runs on every signal.
    Daily-loss and drawdown breaches trip the kill switch, which halts
    trading for the rest of the run (no operator to re-arm it); the
    report states when and why.
-   **Metrics:** total return, max drawdown and its duration,
    annualized volatility, Sharpe (rf = 0), fills, round trips, win
    rate, fees in quote, turnover, time in market (dust excluded), and
    buy-and-hold benchmark gross and net of two fees; plus signal,
    approval, rejection, and expiry counts.
-   **Provenance:** every run is an append-only `backtest_runs` row
    (`ExperimentRecord`): data range, SHA-256 fingerprint of every
    candle read, strategy id/version/config hash/parameters, risk policy
    version, code version (package version + git sha, `.dirty` when
    modified), assumptions, and metrics. Reports (JSON and Markdown)
    carry the caveat that simulated results are not evidence of future
    returns.
-   **Out of sample:** `backtest sweep` runs each parameter combination
    on `[start, split)` and `[split, end)` and shows both side by side,
    with the instruction to choose on in-sample columns only.

### 17.2 Paper broker

Paper broker implements the same `ExecutionPort` and `AccountPort` as
the live CCXT adapter, while market data comes from the real
`MarketDataPort` (public endpoints, no trading credentials). In paper
mode, reconciliation compares local records against the paper broker's
own simulated venue state, exercising the same code paths as live. It
must: - Simulate balances, positions, orders, partial fills if supported
by model, and fees. - Use a documented fill model from the shared
`simulation` module (not simply assume every order fills at candle
close); the backtest broker uses the same model so paper and backtest
results are comparable. - Be deterministic under replay with
a fixed seed/config. - Share risk and order-management logic with live
mode. - Clearly label simulated results and never mix them with real
account data.

### 17.2.1 Implemented paper venue (Phase 6; operator decisions 2026-10-12)

-   **Funding:** the paper account is funded once with
    `[paper] starting_balance` (default 1,000 USDT), recorded in the
    append-only `paper_funding` table, not as trading P&L.
-   **Fill model** (`simulation/fill_model.py`, shared with backtests): IOC
    limit orders fill fully at the best ask (BUY) / bid (SELL) moved against
    the order by `slippage_bps` (default 5), rounded to the tick against the
    order; if that price breaches the limit, the order expires unfilled. A
    `fill_ratio` < 1 exists only to exercise partial-fill handling in tests.
-   **Fees:** `fee_pct` (default 0.1%) of the received asset, like Binance
    spot: BUY fees reduce the BTC received, SELL fees reduce USDT proceeds.
    Selling "the whole position" therefore leaves sub-lot dust, as on the
    real venue; dust counts as flat (§11.1.1).
-   **Venue state** lives in `paper_accounts`, `paper_balances`,
    `paper_orders`, `paper_trades` (append-only), separate from the bot's
    own orders/fills/positions, and each submission commits in the venue's
    own transaction. This gives reconciliation (Phase 7) two independent
    sources to compare, as with a real venue.
-   **Venue rules enforced:** active market, LOT_SIZE, PRICE_FILTER,
    NOTIONAL, funds reserved at the limit price, free base for sells,
    unique client order ID per account.
-   **Signal freshness:** only a signal from the latest closed candle, acted
    on within one timeframe interval of that candle's close, is traded.
-   **Cancellation:** IOC orders are final on submission; nothing rests on
    the paper book, so `cancel_order` reports the final state. The kill
    switch's open-order policy (§24.8) is therefore moot for paper; it
    matters again for live venues where acknowledgments can be delayed.

### 17.3 Sandbox/testnet

Use only after paper behavior is stable. Verify the exchange's sandbox
setup, supported endpoints, symbols, and order semantics. Sandbox
balances and fills are not evidence of live liquidity or profitability.

------------------------------------------------------------------------

## 18. Security and operational safety

-   Store secrets in environment injection or a secrets manager; `.env`
    is local development only and must be gitignored.
-   Provide `.env.example` with placeholders only.
-   Use dedicated exchange API keys, trade-only permissions, withdrawal
    disabled, IP allowlisting where available.
-   Use separate accounts/subaccounts for experimentation and
    production.
-   Restrict network ingress; API should be private or authenticated
    behind TLS/reverse proxy.
-   Run container as non-root; use read-only filesystem where practical
    and explicit writable volumes.
-   Pin image/dependency versions and scan dependencies/images.
-   Never log credentials, authorization headers, or unredacted request
    objects.
-   Apply least privilege to database roles.
-   Back up PostgreSQL and test restoration.
-   Define key rotation and incident response procedures.
-   Avoid storing unnecessary personal or exchange-account data.
-   Live mode requires explicit `TRADING_MODE=live`, explicit
    `LIVE_TRADING_ACK=true`, valid key presence, approved config,
    healthy reconciliation, and no kill switch.
-   Startup should refuse live mode if any safety gate is missing.

------------------------------------------------------------------------

## 19. Observability and alerts

### 19.1 Structured logs

Every event should include, when applicable: - timestamp UTC - level -
component - bot instance - correlation ID - strategy ID/version -
symbol - internal order/intent ID - exchange order ID (when known) -
event name - sanitized details

### 19.2 Metrics

At minimum: - market-data age and ingestion errors - strategy evaluation
duration and lag - signals generated/rejected - risk rejections by
reason - orders by status - submission latency/errors - fill latency and
slippage estimate - reconciliation discrepancies and age - exchange
request latency/rate-limit/errors - DB health/latency - current
exposure, equity estimate, daily P&L, drawdown - kill-switch state -
task heartbeat age

Do not use high-cardinality labels such as order IDs in Prometheus
metrics.

### 19.3 Alert levels

**Info:** routine candle processing, ordinary fills, periodic sync.
Usually dashboard/log only.

**Warning:** transient API errors, delayed data, one retry, approaching
a configured limit. Notify if persistent or actionable.

**Critical:** kill switch tripped, unknown order outcome, reconciliation
failure, unexpected balance/position, database unavailable,
authentication failure, repeated submission errors. Block new orders and
notify.

Alerts should be deduplicated, rate-limited, and include the action
already taken plus a concise remediation hint.

Implemented (Phase 8; operator decisions, 2026-10-13): the channel is
**Telegram**, and only **critical** events are sent; everything else is still
logged and stored. The dispatcher follows `system_events`, so every
component's critical events become alerts with no extra wiring.

- Deduplication: one alert per event and subject per 30 minutes; repeats are
  counted into the next alert.
- Rate limit: 20 alerts per hour; suppressed alerts are summarized in the
  next one.
- Delivery failures are retried for up to an hour and never block a safety
  action.
- Logs are JSON on stderr with secrets redacted. The Telegram token is also
  caught by pattern, since HTTP errors embed it in URLs.
- `arbitron alerts test` checks the channel end to end.

------------------------------------------------------------------------

## 20. Configuration

Use typed settings with validation. Separate common, exchange, strategy,
risk, and deployment config. Prefer TOML/YAML checked into the
repository for non-secret configuration and environment variables for
secrets.

Example conceptual config:

``` toml
[app]
mode = "paper"
environment = "development"
timezone = "UTC"

[exchange]
id = "binance"
sandbox = false
spot_only = true
symbols = ["BTC/USDT"]
request_timeout_ms = 10000

[strategy]
id = "sma_cross"
version = "1"
timeframe = "5m"
enabled = true

[risk]
max_order_notional = "50"
max_asset_exposure = "100"
max_total_exposure = "100"
max_open_orders = 2
max_daily_loss = "10"
max_drawdown_pct = "5"
max_market_data_age_seconds = 600

[execution]
allow_market_orders = false
submission_timeout_seconds = 15
reconciliation_interval_seconds = 60

[live]
enabled = false
acknowledgement = false
```

These values are examples for configuration shape only, not recommended
financial limits. Validate internal consistency and refuse startup on
invalid combinations. Live config should be a separate reviewed file and
should not inherit permissive paper defaults accidentally.

------------------------------------------------------------------------

## 21. Testing strategy

### 21.1 Unit tests

-   Domain validation and Decimal arithmetic.
-   Order state machine transition rules.
-   Strategy determinism, warm-up, and no-lookahead.
-   Sizer rounding, minimums, and sell caps.
-   Risk limit boundaries and rejection reason codes.
-   Exposure including open orders.
-   Position and P&L accounting.
-   Client ID generation and dedupe keys.
-   Configuration validation and live-mode gates.

### 21.2 Adapter contract tests

Use mocked CCXT responses to verify: - market metadata mapping -
precision formatting - normalized statuses - error classification -
capability checks - sanitized logs - adapter closure - exchange-specific
parameter isolation

### 21.3 Integration tests

Use disposable PostgreSQL (e.g. test container or CI service): -
migrations from empty database - repository transactions - unique
constraints/deduplication - event persistence and projection updates -
startup recovery query paths

### 21.4 Failure-injection tests

Must cover: - network timeout before request reaches exchange - timeout
after exchange accepted order but before response reaches bot - rate
limit - authentication failure - database outage - process termination
during submission - duplicate candle/event - partial fill followed by
cancel - cancel/fill race - missing trade history window - stale market
data - unknown exchange order - risk manager exception - reconciliation
disagreement

Expected behavior must be specified per case, especially whether new
orders are blocked and whether the system alerts. The expected-behavior
table (case → resulting order state, trading blocked yes/no and scope,
alert severity, automatic vs operator recovery) is written to
`docs/failure-matrix.md` as the first task of Phase 7 and reviewed
before the tests are implemented.

### 21.5 Test quality gates

CI should run: - formatting/lint - type checking - unit tests -
integration tests - coverage threshold for critical modules -
dependency/security checks

No live exchange calls in ordinary CI. Any manual sandbox test must be
opt-in and clearly separated.

------------------------------------------------------------------------

## 22. Implementation phases and agent workflow

The coding agent must work in small, reviewable phases. At the end of
each phase: run relevant checks, summarize files changed, report tests,
list remaining risks, and stop for review if a design decision or safety
ambiguity blocks progress. Do not implement all phases in one unreviewed
change.

### Phase 0 --- Repository bootstrap

-   [x] Initialize `uv` project, `src/` layout, package metadata,
    lockfile.
-   [x] Add lint, formatting, type-check, and pytest configuration.
-   [x] Add `.gitignore`, `.env.example`, README, and this plan.
-   [x] Add CI workflow for static checks and tests.
-   [x] Add CLI skeleton and validated settings model.
-   [x] Configure `pytest-asyncio` and an async CLI entry point
    (`asyncio.run`) so async is the default from the first commit.

**Acceptance:** fresh clone can install, run CLI help, lint, type-check,
and pass an initial test.

### Phase 1 --- Domain and ports

-   [x] Implement core enums/value objects/entities.
-   [x] Implement `MarketDataPort`, `ExecutionPort`, `AccountPort`,
    repository, clock, and notifier protocols (async methods for I/O).
-   [x] Define the order state machine exactly as specified in §6.2,
    with exhaustive allowed/forbidden transition tests.
-   [x] Define kill-switch states (`ARMED`/`TRIPPED`/`RECOVERY_REVIEW`).
-   [x] Use Decimal and UTC-aware datetimes consistently.

**Acceptance:** domain package imports without CCXT, SQLAlchemy, or
FastAPI; all invariants have unit tests.

### Phase 2 --- Persistence

-   [x] Add async PostgreSQL engine/session lifecycle
    (`postgresql+psycopg`) and the single-instance advisory lock.
-   [x] Use `NUMERIC` columns for all financial values (§5).
-   [x] Implement SQLAlchemy tables and Alembic migrations.
-   [x] Implement repositories and transaction boundaries.
-   [x] Add unique constraints and idempotent insert/upsert patterns.
-   [x] Add append-only order/system event persistence.

**Acceptance:** migrations work from empty DB; repository integration
tests validate transactions, constraints, and deduplication.

### Phase 3 --- CCXT read adapter and market data

-   [x] Implement exchange factory and capability discovery.
-   [x] Add market metadata loading and symbol validation.
-   [x] Implement OHLCV fetch, normalization, persistence, freshness and
    gap checks.
-   [x] Add rate limiting, timeout, retry policy for safe reads.
-   [x] Add mocked adapter contract tests.

**Acceptance:** paper app can ingest and persist closed candles;
stale/invalid data prevents strategy evaluation or order creation.

### Phase 4 --- Strategy and replay

-   [x] Define strategy protocol/context.
-   [x] Implement one simple example strategy.
-   [x] Add event dedupe and deterministic replay.
-   [x] Persist signals with strategy version/config snapshot.
-   [x] Test warm-up and candle-close timing.

**Acceptance:** identical inputs/config produce identical signals; no
order execution is possible from strategy module.

### Phase 5 --- Sizing and risk engine

-   [x] Implement risk policy model and configuration validation.
-   [x] Implement balance/exposure calculations including pending
    orders.
-   [x] Implement the sizer (`sizing` module): sizing policy, precision
    rounding, minimum amount/cost checks.
-   [x] Implement risk re-verification of sizing invariants (reject,
    never resize).
-   [x] Implement durable kill switch and reason-coded decisions.
-   [x] Add exhaustive boundary and failure tests.

**Acceptance:** every order intent requires an approved risk decision;
any risk-state failure blocks orders.

### Phase 6 --- Paper execution and order manager

-   [x] Implement the paper broker (`ExecutionPort` + `AccountPort`)
    on top of the real `MarketDataPort`.
-   [x] Implement intent lifecycle and order state machine.
-   [x] Build `TradingSnapshot` from the portfolio projection, venue, and
    database (marks from last closed candles, fresh ticker, day-start and
    peak equity, orders in the last hour, health flags).
-   [x] Trip the kill switch automatically on `loss_limit_breaches()`.
-   [x] Refuse to act on stale signals (source candle older than the
    latest closed candle, or beyond a configured age), since catch-up
    after downtime persists signals for past candles.
-   [x] Persist before side effects.
-   [x] Implement the shared `simulation` fill/fee model and document
    it.
-   [x] Implement partial fills and cancellation semantics.
-   [x] Add replayable execution tests.

**Acceptance:** end-to-end paper run creates auditable signals, risk
decisions, orders, fills, positions, and snapshots without duplicate
intents.

### Phase 7 --- Reconciliation and recovery

-   [x] Write and review `docs/failure-matrix.md` (§21.4) before
    implementing failure-injection tests.
-   [x] Implement exchange snapshot fetches and normalized
    reconciliation.
-   [x] Implement startup recovery sequence.
-   [x] Implement periodic reconciliation and overlap/dedupe for fills.
-   [x] Add `SUBMISSION_UNKNOWN` handling.
-   [x] Add discrepancy classification and recovery-review state.
-   [x] Add failure-injection tests for ambiguous outcomes.

**Acceptance:** restart with open orders and partial fills recovers
safely; unresolved discrepancies block new orders and raise critical
alerts.

### Phase 8 --- Supervisor and observability

-   [x] Implement task supervisor, health registry, heartbeats, graceful
    shutdown.
-   [x] Add structured logging and redaction.
-   [x] Add Prometheus metrics and health/readiness endpoints.
-   [x] Add notifier abstraction and alert dedupe/rate limiting.
-   [x] Verify critical task failure trips safety policy.

**Acceptance:** simulated task/dependency failures produce expected safe
state and observable alerts; shutdown closes resources cleanly.

### Phase 9 --- Backtest and research harness

-   [x] Implement historical data loader and backtest broker.
-   [x] Reuse the shared `simulation` fill model; extend it for spread,
    slippage, and latency assumptions rather than forking it.
-   [x] Add performance and risk metrics.
-   [x] Add experiment metadata/versioning and report export.
-   [x] Add tests against hand-calculated toy datasets.

**Acceptance:** backtest is deterministic, avoids lookahead, and reports
assumptions and costs alongside results.

### Phase 10 --- API and deployment

-   [x] Add authenticated read-only operational API.
-   [x] Add audited operator actions, especially kill switch.
-   [x] Add Dockerfile and Compose for app + PostgreSQL.
-   [x] Add health checks, volumes, resource limits, restart policy.
-   [x] Document backup/restore and deployment procedure.
-   [x] Add optional Grafana/Prometheus configuration.

**Acceptance:** deployment starts in paper mode, health checks work,
data persists across container restart, and API cannot be used
unauthenticated to change trading state.

### Phase 11 --- Sandbox qualification

-   [x] Select one exchange and verify sandbox availability and
    limitations.
-   [x] Configure separate sandbox credentials.
-   [x] Run manual end-to-end order lifecycle tests.
-   [x] Verify precision, minimums, client IDs, cancellation, fills, and
    reconciliation.
-   [x] Document any unsupported endpoint/behavior.

Results, fixes, and limitations: `docs/sandbox-qualification.md`.

**Acceptance:** sandbox tests pass for the chosen exchange and all
limitations are recorded. No live keys are used.

### Phase 12 --- Controlled live-readiness review

This phase is a review gate, not an automatic deployment.

-   [ ] Complete a sustained paper soak test with predefined duration
    and success thresholds.
-   [ ] Review strategy evaluation methodology and out-of-sample
    results.
-   [ ] Review risk limits and kill-switch behavior.
-   [ ] Review exchange account permissions and key storage.
-   [ ] Perform recovery drill and database restore drill.
-   [ ] Review alert routing and operator runbooks.
-   [ ] Verify live mode cannot start without all explicit gates.
-   [ ] Obtain human approval before enabling any live order path.

**Acceptance:** signed-off checklist and documented residual risks. The
coding agent must not enable live trading or choose financial limits
autonomously.

------------------------------------------------------------------------

## 23. Operational runbooks

Create `docs/runbooks/` with:

-   `startup.md` --- prerequisites, config validation, startup and
    readiness.
-   `shutdown.md` --- graceful stop and order policy.
-   `recovery.md` --- restart, unknown submission, discrepancy review.
-   `kill-switch.md` --- trip, verify, acknowledge, recovery procedure.
-   `exchange-outage.md` --- pause behavior and recovery.
-   `database-restore.md` --- backup verification and restore.
-   `credential-rotation.md` --- revoke/replace keys and validate
    permissions.
-   `incident-response.md` --- preserve logs/events, stop trading,
    investigate, resume criteria.

Each runbook should distinguish automated actions from operator-only
actions and include verification steps.

------------------------------------------------------------------------

## 24. Important unresolved decisions

Do not guess these; use safe defaults and document assumptions:

1.  **Exchange:** **decided: Binance spot** (operator is in India).
    Public market data via CCXT is verified (Phase 3). Still to verify
    before live use: account eligibility and KYC for the operator's
    jurisdiction and API key permissions. Sandbox: **decided and qualified**
    on the Binance spot testnet (Phase 11, `docs/sandbox-qualification.md`).
    Open for Phase 12: a dedicated account that starts flat in the
    configured base assets, or a recorded opening balance in reconciliation.
2.  **Market:** spot only for MVP; confirm quote currency and symbol
    allowlist.
3.  **Strategy:** select only for software integration initially; do not
    infer profitability.
4.  **Timeframe:** configure per strategy and test candle finalization
    behavior.
5.  **Risk limits:** user/operator must set values; no universal
    defaults should be treated as safe.
6.  **Valuation source:** **decided** (§11.1.1): last closed candle close.
7.  **Order policy:** **decided** (§11.1.1): IOC marketable limits within a
    slippage cap; no replacement of unfilled IOC remainders (a new signal
    is needed).
8.  **Existing-order response to kill switch:** define whether to cancel
    entries, retain exits, or require review. (Moot while all orders are
    IOC; revisit before any resting-order type is introduced.)
9.  **Alert channel:** **decided:** Telegram, critical events only
    (`ARBITRON_TELEGRAM_BOT_TOKEN` and `ARBITRON_TELEGRAM_CHAT_ID`).
10. **Hosting and backups:** select provider, region, access model,
    backup retention, and restore objective.
11. **Jurisdictional requirements:** verify applicable exchange terms,
    tax/reporting obligations, and local rules with qualified sources
    before live use. For India this includes the tax treatment of virtual
    digital assets and TDS on transfers; confirm with a qualified tax
    professional, as the bot's accounting must support whatever records
    are required.

The agent may propose options, but must not silently decide high-impact
financial or operational policy.

------------------------------------------------------------------------

## 25. Coding-agent instructions

When implementing this plan:

-   Read the entire plan before editing.
-   First inspect the repository and report existing structure,
    conventions, and constraints.
-   Propose the next phase only; do not jump ahead without satisfying
    dependencies.
-   Keep changes small and cohesive.
-   Do not introduce unnecessary frameworks or microservices.
-   Do not expose CCXT types outside the adapter.
-   Do not use `float` for financial values in domain logic.
-   Do not make network calls from strategy or risk code.
-   Do not make live order calls in unit tests or ordinary CI.
-   Do not store secrets in source, fixtures, logs, or committed config.
-   Add tests with each feature, including negative and failure cases.
-   Update docs and examples whenever behavior/config changes.
-   Never enable withdrawals, leverage, or live trading as a
    convenience.
-   Never treat a successful backtest or sandbox run as proof of future
    returns.
-   If a requirement is ambiguous, choose the safer behavior, document
    the assumption, and ask for clarification when it affects money
    movement or recovery semantics.
-   At the end of each phase, report: implementation summary, files
    changed, commands/tests run, results, known limitations, and the
    exact next phase.

## 26. Final release checklist

### Correctness

-   [ ] UTC-aware timestamps throughout.
-   [ ] Decimal financial arithmetic and explicit rounding.
-   [ ] Deterministic signal deduplication.
-   [ ] Centralized order state machine.
-   [ ] Immutable/deduplicated fill facts.
-   [ ] Correct partial-fill and cancel/fill-race handling.

### Safety

-   [ ] Live mode disabled by default.
-   [ ] Risk approval required for every order.
-   [ ] Fail-closed on stale data, DB failure, unhealthy exchange, and
    unresolved reconciliation.
-   [ ] Durable kill switch and audited operator actions.
-   [ ] No withdrawal permission.
-   [ ] Ambiguous submission never triggers blind retry.

### Recovery

-   [ ] Startup sync and reconciliation.
-   [ ] Periodic reconciliation.
-   [ ] Recovery review on unknown orders/discrepancies.
-   [ ] Restart and restore drills completed.

### Operations

-   [ ] Health/readiness endpoints.
-   [ ] Heartbeats and metrics.
-   [ ] Structured redacted logs.
-   [ ] Actionable alerts.
-   [ ] Backup/restore and incident runbooks.
-   [ ] Single-instance protection.

### Validation

-   [ ] Unit, integration, contract, and failure-injection tests.
-   [ ] Backtest assumptions documented.
-   [ ] Paper soak test completed.
-   [ ] Sandbox qualification completed for chosen venue.
-   [ ] Human live-readiness review completed.

**Release principle:** If the system cannot confidently establish its
state, it must stop opening new positions, preserve evidence, and
request review rather than guessing.
