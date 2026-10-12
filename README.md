# arbitron

Autonomous spot crypto trading bot. It runs in backtest and paper modes first,
and its design rule is **fail closed**: if it can't establish its state with
confidence, it stops opening positions and asks for review.

> **Status:** Phase 5 (sizing and risk gate) complete. There is no trading logic yet.
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
(for example, arming a tripped kill switch without a review first).

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
