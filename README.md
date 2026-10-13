# arbitron

Autonomous spot crypto trading bot. It runs in backtest and paper modes first,
and its design rule is **fail closed**: if it can't establish its state with
confidence, it stops opening positions and asks for review.

> **Status:** Phase 8 (supervision, monitoring, alerts) complete. Only simulated trading is possible.
> Live mode can't be selected. See [`plan.md`](plan.md) for the full
> specification and phased roadmap.

Nothing in this repository promises profitability. Backtest, paper, and sandbox
results are not evidence of future returns.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
uv sync
uv run arbitron --help
```

## Configuration

Settings that aren't secret go in a TOML file. Secrets go **only** in
environment variables.

```bash
cp configs/example.paper.toml configs/paper.toml   # local copies are gitignored
uv run arbitron check-config configs/paper.toml
```

- The example values show the shape of the config only. They are not
  recommended financial limits.
- Write financial values as quoted strings (`"50"`). Unquoted floats are
  rejected, because floats would introduce binary rounding error.
- Validation rejects unknown keys, inconsistent limits, non-UTC timezones,
  non-spot markets, and any attempt to select live mode.
- Paper and backtest modes need no exchange credentials. For secrets, copy
  `.env.example` to `.env`, which is for local development only and is
  gitignored.

## Database

PostgreSQL 18 with the async psycopg driver. For local development:

```bash
docker compose -f deploy/compose.dev.yaml up -d
export ARBITRON_DATABASE_URL=postgresql+psycopg://arbitron:arbitron@localhost:5432/arbitron
uv run arbitron db upgrade    # apply migrations
uv run arbitron db status     # verify the schema matches this build
```

Create a migration after changing `src/arbitron/persistence/tables.py`:

```bash
uv run alembic revision --autogenerate -m "describe the change"
```

Review every generated migration by hand: autogenerate doesn't detect
changes to CHECK constraints or triggers.

## Market data

Ingest closed candles for the configured symbols from Binance's public API
(no credentials needed), then run the data-quality gate:

```bash
uv run arbitron market sync configs/paper.toml            # one cycle
uv run arbitron market sync configs/paper.toml --follow   # after every candle close
```

Exit codes: `0` ok, `1` invalid config, `2` database error, `3` the data-quality
gate is blocking (stale data, missing history, clock skew, inactive market, ...),
`4` venue request failed, `5` the request isn't allowed in the current state
(for example, arming a tripped kill switch without a review first), `6` a
supervised session stopped because of a failure.

## Strategy signals

The configured strategy turns finalized candles into buy/sell **signals**. No
orders are created yet. The bundled `sma_cross` strategy is a software
integration fixture, not a trading recommendation.

```bash
uv run arbitron strategy run configs/paper.toml            # sync data, evaluate new candles
uv run arbitron strategy run configs/paper.toml --follow   # after every candle close
uv run arbitron strategy replay configs/paper.toml --start 2026-10-01T00:00:00Z
```

- `run` evaluates only when the data-quality gate passes. On first run it
  evaluates only the latest closed candle; afterwards it evaluates every new
  candle, and records signals and its progress in the database.
- `replay` prints what the strategy would have signaled over stored candles
  and writes nothing. It uses the same evaluation code as `run`, so the two
  agree.
- Strategy parameters live under `[strategy.parameters]`. The configured
  `version` must match the strategy code's version, or the config is
  rejected.

## Sizing, risk, and the kill switch

Every order intent is sized by `[sizing]` and must be approved by the risk
gate (`[risk]`) before it can go anywhere. The database itself refuses to
approve an order without a recorded risk decision.

- Sizing: a fixed quote amount per entry; buy only when flat; sell the whole
  position; immediate-or-cancel limit orders within `max_slippage_pct` of
  the book.
- The risk limits have no defaults. The example values are for paper
  trading only. Daily loss, drawdown, exposure and frequency limits block
  new entries, never exits.

The kill switch starts in `recovery_review`. Nothing trades until a named
operator arms it:

```bash
uv run arbitron killswitch status
uv run arbitron killswitch arm --operator NAME --ack "what you reviewed"
uv run arbitron killswitch trip --operator NAME --reason "why"     # emergency stop
uv run arbitron killswitch review --operator NAME --reason "..."   # required before re-arming
```

## Paper trading

A simulated venue trades on live Binance prices with no real funds. Each cycle
syncs market data, evaluates the strategy, records equity, trips the kill
switch on a daily-loss or drawdown breach, and acts on a fresh signal: size it,
risk-check it, submit it, record the fill and the position.

```bash
uv run arbitron killswitch arm --operator NAME --ack "paper limits reviewed"
uv run arbitron paper run configs/paper.toml --follow   # one cycle per candle close
uv run arbitron paper status configs/paper.toml         # balances, positions, equity, orders
```

- The paper account is funded once with `[paper] starting_balance` (default 1000 USDT).
- Fill model: the order fills at the best ask or bid moved against you by
  `slippage_bps`, and never past its limit. Fees are charged in the asset you
  receive, so selling "everything" leaves sub-lot dust, as on Binance.
- Only a signal from the latest closed candle is traded, and only within one
  candle interval of that candle closing.
- An ambiguous submission (timeout, network error) leaves the order
  `submission_unknown` and blocks new orders until reconciliation (Phase 7).
- Results are simulated. They aren't evidence that the strategy would make
  money.

## Reconciliation and recovery

The bot regularly compares its own records with the venue's. It does this at
startup, every `execution.reconciliation_interval_seconds`, and straight after
any ambiguous submission. `docs/failure-matrix.md` lists every failure case
and the expected behavior, as approved by the operator.

- If a submission's outcome is unknown, the bot looks the order up on the
  venue. If the order is found, the bot applies the venue's state. If it
  isn't, the order becomes `not_placed` only after 30 seconds and two missed
  lookups. After 15 minutes unresolved, it escalates to the operator.
- New orders stay blocked until reconciliation is clean.
- Anything the bot can't safely repair (holdings that don't match its
  position, an unknown order on the venue, contradictory fills) moves the
  kill switch to `recovery_review`. A named operator has to review and re-arm
  it.

```bash
uv run arbitron orders unresolved configs/paper.toml
uv run arbitron orders resolve configs/paper.toml ORDER_ID --as not_placed \
    --operator NAME --note "evidence"
```

`orders resolve` only closes an order as unfilled. It refuses if the venue
has any record of the order: fills only ever come from venue data.

## Running unattended: supervision, monitoring, alerts

`paper run --follow` runs a supervised process:

- Separate tasks handle trading, reconciliation, the instance-lock check,
  alerts, heartbeats and the HTTP endpoints.
- A crashed critical task is restarted up to 3 times in 10 minutes. One more
  crash trips the kill switch and stops the session.
- SIGINT and SIGTERM shut it down cleanly.

The operational API is served on `127.0.0.1:9108`, configured under
`[monitoring]`. It needs a bearer token: set `ARBITRON_API_TOKEN` to at least
32 random characters (for example `openssl rand -hex 32`). `paper run --follow`
refuses to start without one.

- `GET /health` is the only endpoint that works without the token. It
  returns `{"status": "alive"}` and nothing else.
- `GET /ready` returns 200 only when it's safe to trade. Otherwise it returns
  503 with the failing checks (for example, the kill switch isn't armed).
- `GET /status`, `/portfolio`, `/positions`, `/orders`, `/signals`,
  `/risk/decisions` and `/system/events` are read-only views of the bot's
  state.
- `GET /metrics` serves Prometheus metrics: venue calls, data freshness,
  signals, orders, risk rejections, reconciliation, equity, kill-switch state
  and heartbeats.
- `POST /kill-switch/trip` and `POST /reconciliation` are the only actions.
  Both are audited. Arming and order resolution are CLI-only.

```sh
curl -H "Authorization: Bearer $ARBITRON_API_TOKEN" http://127.0.0.1:9108/status
```

Logs are JSON lines on stderr, with secrets redacted.

### Telegram alerts

Critical events (kill switch tripped, unknown orders, reconciliation
discrepancies, task crashes, authentication failures) are sent to Telegram.
Each alert says what happened, what the bot already did, and what to do next.

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Send your bot any message, then find your chat id at
   `https://api.telegram.org/bot<TOKEN>/getUpdates`.
3. Set `ARBITRON_TELEGRAM_BOT_TOKEN` and `ARBITRON_TELEGRAM_CHAT_ID` in the
   environment (or `.env`), and `[alerts] channel = "telegram"` in your config.
4. Check delivery: `uv run arbitron alerts test configs/paper.toml`

With `channel = "telegram"`, `paper run --follow` refuses to start without
those two secrets. The `log` channel only logs alerts; use it for trying
things out, not for unattended running.

## Deployment

`deploy/compose.yaml` runs the app, PostgreSQL and daily backups as one
Docker Compose stack, in paper mode. Prometheus and Grafana are optional.
Every port binds to localhost; you reach them over an SSH tunnel. Secrets are
files and are never environment variables.

    deploy/setup.sh
    docker compose -f deploy/compose.yaml up -d --build

[docs/deployment.md](docs/deployment.md) covers first deployment, arming,
the API, upgrades, backup and restore, secret rotation and incidents.

## Backtesting

Backtests read only from the database, so first backfill history from Binance
(public endpoints, no credentials). This also stores the market rules (lot and
tick sizes, minimums):

    uv run arbitron market backfill configs/paper.toml --start 2026-09-01 --end 2026-10-13

Then run a backtest of the configured strategy, optionally overriding its
parameters:

    uv run arbitron backtest run configs/paper.toml --start 2026-09-15 --end 2026-10-13 \
        --param fast=10 --out reports/

How it models trading:

- A decision made at a candle's close fills at the next candle's open. If that
  candle is missing, the order expires.
- Fills use a quote around the open (`spread_bps`), slippage (`slippage_bps`)
  and fees (`fee_pct`), set under `[backtest]`. Fills go through the same fill
  model as paper trading.
- The full sizing and risk policy applies. A loss-limit breach trips the kill
  switch and halts trading for the rest of the run.

Every run is recorded with its data fingerprint, parameters, risk policy, code
version and assumptions. Use `backtest list` and `backtest show ID` to see
past runs.

To compare parameters without fooling yourself, sweep them over an in-sample
period and check the out-of-sample period:

    uv run arbitron backtest sweep configs/paper.toml --start 2026-09-15 \
        --split 2026-10-01 --end 2026-10-13 --param fast=5,10,20 --param slow=30,60

Pick parameters by the in-sample columns only. Backtest results are simulated
and are not evidence of future returns.

## Development

```bash
uv run ruff format .          # format
uv run ruff check .           # lint
uv run pyright                # type-check (strict)
uv run pytest --cov           # all tests with coverage (integration needs Docker)
uv run pytest -m "not integration"   # unit + contract tests only
ARBITRON_LIVE_EXCHANGE_TESTS=1 uv run pytest tests/live   # opt-in: real Binance API
```

Integration tests start a throwaway PostgreSQL container through
testcontainers. To use an existing server, set `ARBITRON_TEST_DATABASE_URL`.
Its database name must end in `_test`, because the tests truncate every
table.

CI runs the same checks on every push and pull request
(`.github/workflows/ci.yml`).
