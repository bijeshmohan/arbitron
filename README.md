# arbitron

Autonomous spot crypto trading bot. It runs in backtest and paper modes first,
and its design rule is **fail closed**: if it can't establish its state with
confidence, it stops opening positions and asks for review.

> **Status:** Phase 0 (repository bootstrap). There is no trading logic yet.
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
- Paper and backtest modes need no credentials. For other modes, copy
  `.env.example` to `.env`, which is for local development only and is
  gitignored.

## Development

```bash
uv run ruff format .          # format
uv run ruff check .           # lint
uv run pyright                # type-check (strict)
uv run pytest --cov           # tests with coverage
```

CI runs the same checks on every push and pull request
(`.github/workflows/ci.yml`).
