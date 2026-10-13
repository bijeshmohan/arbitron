"""Human- and machine-readable backtest reports (plan §17.1).

Every report states its assumptions, data range, data fingerprint, and code
and strategy versions next to the results, and carries the standing caveat
that simulated results are not evidence of future returns.
"""

import json
from collections.abc import Sequence
from typing import Protocol

from arbitron.domain.experiments import ExperimentRecord


class SweepLike(Protocol):
    @property
    def parameters(self) -> str: ...
    @property
    def in_sample(self) -> ExperimentRecord | None: ...
    @property
    def out_of_sample(self) -> ExperimentRecord | None: ...
    @property
    def error(self) -> str | None: ...


CAVEAT = (
    "Simulated results on historical data. Costs and fills are modeled, not observed. "
    "Past performance in a backtest is not evidence of future returns."
)

_HEADLINE = (
    ("total_return_pct", "Total return %"),
    ("benchmark_buy_hold_net_pct", "Buy & hold (net of fees) %"),
    ("max_drawdown_pct", "Max drawdown %"),
    ("max_drawdown_duration", "Max drawdown duration"),
    ("volatility_annualized_pct", "Volatility (annualized) %"),
    ("sharpe_ratio_rf0", "Sharpe (rf = 0)"),
    ("trades", "Fills"),
    ("round_trips", "Round trips"),
    ("win_rate_pct", "Win rate %"),
    ("fees_paid", "Fees paid (quote)"),
    ("turnover", "Turnover (x avg equity)"),
    ("exposure_pct", "Time in market %"),
    ("ending_equity", "Ending equity"),
)


def to_json(record: ExperimentRecord) -> str:
    return json.dumps(
        {
            "id": str(record.id),
            "created_at": record.created_at.isoformat(),
            "label": record.label,
            "exchange_id": record.exchange_id,
            "symbols": list(record.symbols),
            "timeframe": record.timeframe,
            "start": record.start.isoformat(),
            "end": record.end.isoformat(),
            "strategy": {
                "id": record.strategy_id,
                "version": record.strategy_version,
                "config_hash": record.config_hash,
                "parameters": dict(record.parameters),
            },
            "risk_policy_version": record.risk_policy_version,
            "code_version": record.code_version,
            "data": {"fingerprint": record.data_fingerprint, "candles": record.candle_count},
            "assumptions": dict(record.assumptions),
            "metrics": dict(record.metrics),
            "caveat": CAVEAT,
        },
        indent=2,
        sort_keys=True,
    )


def to_markdown(record: ExperimentRecord) -> str:
    m = record.metrics
    params = ", ".join(f"{k}={v}" for k, v in sorted(record.parameters.items())) or "-"
    lines = [
        f"# Backtest {record.id}",
        "",
        f"> {CAVEAT}",
        "",
        "## Setup",
        "",
        f"- Strategy: `{record.strategy_id}` v{record.strategy_version} ({params})",
        f"- Market: {record.exchange_id} {', '.join(record.symbols)} [{record.timeframe}]",
        f"- Period: {record.start.isoformat()} to {record.end.isoformat()}",
        f"- Data: {record.candle_count} candles, fingerprint `{record.data_fingerprint[:16]}`",
        f"- Risk policy: `{record.risk_policy_version}`; code: `{record.code_version}`",
        "",
        "## Assumptions",
        "",
        *(f"- {k.replace('_', ' ')}: {v}" for k, v in sorted(record.assumptions.items())),
        "",
        "## Results",
        "",
        "| Metric | Value |",
        "|---|---|",
        *(
            f"| {label} | {m.get(key) if m.get(key) is not None else '-'} |"
            for key, label in _HEADLINE
        ),
        "",
        "## Decisions",
        "",
        f"- Signals: {m.get('signals')}; approved: {m.get('orders_approved')}; "
        f"expired unfilled: {m.get('orders_expired')}; rejected: {m.get('orders_rejected')}",
        f"- Rejection reasons: {m.get('rejection_reasons') or '-'}",
        f"- Not sized: {m.get('not_sized') or '-'}",
        f"- Candles skipped (incomplete history): {m.get('skipped_windows')}",
    ]
    if m.get("halted_at"):
        lines.append(
            f"- **Kill switch halted trading at {m.get('halted_at')}**: {m.get('halt_reason')}"
        )
    return "\n".join(lines) + "\n"


def sweep_table(rows: Sequence[SweepLike]) -> str:
    """In-sample vs out-of-sample side by side, one row per parameter set."""
    header = [
        "| Parameters | IS return % | IS max DD % | IS trades "
        "| OOS return % | OOS max DD % | OOS trades |",
        "|---|---|---|---|---|---|---|",
    ]
    body: list[str] = []
    for row in rows:
        if row.in_sample is None or row.out_of_sample is None:
            body.append(f"| {row.parameters} | invalid: {row.error} | | | | | |")
            continue
        i, o = row.in_sample.metrics, row.out_of_sample.metrics
        body.append(
            f"| {row.parameters} | {i.get('total_return_pct')} | {i.get('max_drawdown_pct')} | "
            f"{i.get('trades')} | {o.get('total_return_pct')} | {o.get('max_drawdown_pct')} | "
            f"{o.get('trades')} |"
        )
    note = (
        "\nChoose parameters on the in-sample columns only; the out-of-sample columns "
        "show how that choice held up. Selecting on out-of-sample results turns them into "
        "in-sample results.\n"
    )
    return "\n".join([*header, *body]) + "\n" + note
