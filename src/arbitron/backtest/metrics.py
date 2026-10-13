"""Performance and risk metrics for a backtest (plan §17.1).

All arithmetic is exact ``Decimal`` (including the square root for
volatility). Values are returned as strings rounded for display; percentages
are percentages (``"12.5"`` means 12.5%).

- ``total_return_pct``: end equity vs starting balance.
- ``max_drawdown_pct`` / ``max_drawdown_duration``: worst peak-to-trough fall
  of the equity curve (marked at candle closes) and how long it lasted.
- ``volatility_annualized_pct``: standard deviation of per-candle returns,
  scaled by sqrt(candles per year).
- ``sharpe_ratio_rf0``: annualized mean / std of per-candle returns with a
  zero risk-free rate. A rough comparative figure, not a forecast.
- ``turnover``: traded notional / average equity.
- ``exposure_pct``: share of candle closes holding a tradeable position
  (sub-lot dust left after selling does not count).
- ``benchmark_buy_hold_pct``: buy at the first open, hold to the last close
  (gross); ``..._net_pct`` subtracts one fee on entry and one on exit.
"""

import itertools
from collections.abc import Mapping, Sequence
from datetime import timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from arbitron.backtest.engine import Assumptions, Result, TradeRecord
from arbitron.domain.enums import Side
from arbitron.domain.market import Candle, Timeframe
from arbitron.domain.money import FINANCIAL_CONTEXT, canonical_str

_YEAR = timedelta(days=365)
_HUNDRED = Decimal(100)
_DISPLAY = Decimal("0.0001")

type MetricValue = str | int | None


def _fmt(value: Decimal) -> str:
    return canonical_str(value.quantize(_DISPLAY, rounding=ROUND_HALF_EVEN))


def compute(
    result: Result,
    assumptions: Assumptions,
    *,
    timeframe: Timeframe,
    candles: Mapping[str, Sequence[Candle]],
) -> dict[str, MetricValue]:
    metrics: dict[str, MetricValue] = {}
    start = assumptions.starting_balance
    with localcontext(FINANCIAL_CONTEXT):
        equity = [p.equity for p in result.equity]
        end = equity[-1] if equity else start
        metrics["starting_balance"] = _fmt(start)
        metrics["ending_equity"] = _fmt(end)
        metrics["total_return_pct"] = _fmt((end / start - 1) * _HUNDRED)
        metrics.update(_drawdown(result, timeframe))
        metrics.update(_volatility(equity, start, timeframe))
        metrics.update(_trading(result, assumptions))
        metrics["exposure_pct"] = (
            _fmt(
                Decimal(sum(1 for p in result.equity if p.in_market))
                / len(result.equity)
                * _HUNDRED
            )
            if result.equity
            else None
        )
        metrics.update(_benchmark(candles, result, assumptions))
    metrics["candles_evaluated"] = len(result.equity)
    metrics["signals"] = result.signals
    metrics["orders_approved"] = result.approved
    metrics["orders_expired"] = result.expired
    metrics["orders_rejected"] = sum(result.rejected.values())
    metrics["rejection_reasons"] = (
        ",".join(f"{k}={v}" for k, v in sorted(result.rejected.items())) or None
    )
    metrics["not_sized"] = ",".join(f"{k}={v}" for k, v in sorted(result.not_sized.items())) or None
    metrics["skipped_windows"] = result.skipped_windows
    metrics["halted_at"] = result.halted_at.isoformat() if result.halted_at else None
    metrics["halt_reason"] = result.halt_reason
    return metrics


def _drawdown(result: Result, timeframe: Timeframe) -> dict[str, MetricValue]:
    peak: Decimal | None = None
    peak_index = 0
    worst = Decimal(0)
    longest = 0
    for index, point in enumerate(result.equity):
        if peak is None or point.equity >= peak:
            peak, peak_index = point.equity, index
            continue
        drawdown = (peak - point.equity) / peak * _HUNDRED if peak else Decimal(0)
        worst = max(worst, drawdown)
        longest = max(longest, index - peak_index)
    return {
        "max_drawdown_pct": _fmt(worst),
        "max_drawdown_duration": str(timeframe.duration * longest),
    }


def _volatility(
    equity: Sequence[Decimal], start: Decimal, timeframe: Timeframe
) -> dict[str, MetricValue]:
    series = [start, *equity]
    returns = [b / a - 1 for a, b in itertools.pairwise(series) if a]
    if len(returns) < 2:
        return {"volatility_annualized_pct": None, "sharpe_ratio_rf0": None}
    mean = sum(returns, Decimal(0)) / len(returns)
    variance = sum(((r - mean) ** 2 for r in returns), Decimal(0)) / (len(returns) - 1)
    std = variance.sqrt()
    periods_per_year = Decimal(_YEAR / timeframe.duration)
    annual_std = std * periods_per_year.sqrt()
    sharpe = (mean * periods_per_year) / annual_std if annual_std else None
    return {
        "volatility_annualized_pct": _fmt(annual_std * _HUNDRED),
        "sharpe_ratio_rf0": _fmt(sharpe) if sharpe is not None else None,
    }


def _trading(result: Result, assumptions: Assumptions) -> dict[str, MetricValue]:
    trades = result.trades
    fees = sum((_fee_in_quote(t) for t in trades), Decimal(0))
    notional = sum((t.amount * t.price for t in trades), Decimal(0))
    average_equity = (
        sum((p.equity for p in result.equity), Decimal(0)) / len(result.equity)
        if result.equity
        else assumptions.starting_balance
    )
    exits = [t for t in trades if t.side is Side.SELL]
    wins = sum(1 for t in exits if t.realized_pnl_change > 0)
    return {
        "trades": len(trades),
        "round_trips": len(exits),
        "win_rate_pct": _fmt(Decimal(wins) / len(exits) * _HUNDRED) if exits else None,
        "fees_paid": _fmt(fees),
        "traded_notional": _fmt(notional),
        "turnover": _fmt(notional / average_equity) if average_equity else None,
    }


def _fee_in_quote(trade: TradeRecord) -> Decimal:
    """Fees paid in the base asset are valued at the trade price."""
    quote = trade.symbol.split("/")[1]
    return trade.fee if trade.fee_currency == quote else trade.fee * trade.price


def _benchmark(
    candles: Mapping[str, Sequence[Candle]], result: Result, assumptions: Assumptions
) -> dict[str, MetricValue]:
    if not result.equity or len(candles) != 1:
        return {"benchmark_buy_hold_pct": None, "benchmark_buy_hold_net_pct": None}
    first_close = result.equity[0].time
    last_close = result.equity[-1].time
    series = next(iter(candles.values()))
    in_range = [c for c in series if first_close <= c.close_time <= last_close]
    if not in_range:
        return {"benchmark_buy_hold_pct": None, "benchmark_buy_hold_net_pct": None}
    gross = in_range[-1].close / in_range[0].open - 1
    fee = assumptions.fee_rate
    net = (1 + gross) * (1 - fee) * (1 - fee) - 1
    return {
        "benchmark_buy_hold_pct": _fmt(gross * _HUNDRED),
        "benchmark_buy_hold_net_pct": _fmt(net * _HUNDRED),
    }
