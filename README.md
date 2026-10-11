# arbitron

Autonomous spot crypto trading bot. It runs in backtest and paper modes first,
and its design rule is **fail closed**: if it can't establish its state with
confidence, it stops opening positions and asks for review.

> **Status:** Phase 2 (persistence) complete. There is no trading logic yet.
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

## Development

```bash
uv run ruff format .          # format
uv run ruff check .           # lint
uv run pyright                # type-check (strict)
uv run pytest --cov           # all tests with coverage (integration needs Docker)
uv run pytest -m "not integration"   # unit tests only
```

Integration tests start a throwaway PostgreSQL container through
testcontainers. To use an existing server, set `ARBITRON_TEST_DATABASE_URL`.
Its database name must end in `_test`, because the tests truncate every
table.

CI runs the same checks on every push and pull request
(`.github/workflows/ci.yml`).
