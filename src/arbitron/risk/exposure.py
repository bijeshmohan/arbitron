"""Valuation and worst-case exposure over a ``TradingSnapshot`` (plan §11.2, §13).

Valuation source: each base asset is marked at its symbol's last closed candle
close (``snapshot.marks``). Only the quote currency and the configured
symbols' base assets are valued; anything else held (e.g. dust) is ignored and
reported, consistently for every calculation, so daily-loss and drawdown
comparisons are like for like.

Exposure is worst case: held base value plus the full remaining notional of
open BUY orders at their limit prices. Open SELL orders do not reduce exposure
until they fill.
"""

from dataclasses import dataclass
from decimal import Decimal, localcontext

from arbitron.domain.enums import Side
from arbitron.domain.money import FINANCIAL_CONTEXT
from arbitron.domain.snapshot import UNACKNOWLEDGED_STATUSES, TradingSnapshot


class ValuationUnavailableError(Exception):
    """A required mark is missing, so equity or exposure cannot be computed."""


@dataclass(frozen=True, slots=True)
class Valuation:
    equity: Decimal
    unvalued_assets: tuple[str, ...]  # held assets outside the configured markets


def valuation(snapshot: TradingSnapshot) -> Valuation:
    """Equity in the quote currency. Raises ``ValuationUnavailableError`` if a held
    configured asset has no mark."""
    with localcontext(FINANCIAL_CONTEXT):
        quote = snapshot.quote_balance()
        equity = quote.total if quote is not None else Decimal(0)
        valued = {snapshot.quote_currency}
        for symbol, market in snapshot.markets.items():
            valued.add(market.base)
            held = snapshot.held_quantity(symbol)
            if held == 0:
                continue
            mark = snapshot.marks.get(symbol)
            if mark is None:
                msg = f"no mark price for {symbol} while holding {held} {market.base}"
                raise ValuationUnavailableError(msg)
            equity += held * mark
    unvalued = tuple(
        sorted(asset for asset, b in snapshot.balances.items() if asset not in valued and b.total)
    )
    return Valuation(equity, unvalued)


def asset_exposure(snapshot: TradingSnapshot, symbol: str) -> Decimal:
    """Worst-case quote value committed to ``symbol``'s base asset."""
    with localcontext(FINANCIAL_CONTEXT):
        held = snapshot.held_quantity(symbol)
        exposure = Decimal(0)
        if held:
            mark = snapshot.marks.get(symbol)
            if mark is None:
                msg = f"no mark price for {symbol}"
                raise ValuationUnavailableError(msg)
            exposure = held * mark
        for order in snapshot.open_orders_for(symbol):
            if order.intent.side is Side.BUY:
                exposure += order.remaining_amount * _limit(order.intent.limit_price, symbol)
        return exposure


def total_exposure(snapshot: TradingSnapshot) -> Decimal:
    with localcontext(FINANCIAL_CONTEXT):
        return sum((asset_exposure(snapshot, s) for s in snapshot.markets), Decimal(0))


def committed_quote(snapshot: TradingSnapshot, fee_rate: Decimal) -> Decimal:
    """Quote reserved by BUY orders the venue has not acknowledged yet (incl. fees).

    Acknowledged orders are already reflected in the exchange's ``used`` balance.
    """
    with localcontext(FINANCIAL_CONTEXT):
        total = Decimal(0)
        for order in snapshot.open_orders:
            if order.intent.side is Side.BUY and order.status in UNACKNOWLEDGED_STATUSES:
                notional = order.remaining_amount * _limit(
                    order.intent.limit_price, order.intent.symbol
                )
                total += notional * (1 + fee_rate)
        return total


def _limit(price: Decimal | None, symbol: str) -> Decimal:
    if price is None:
        # Market orders have no worst-case price; this build never creates them.
        msg = f"open market order on {symbol} has no bounded price"
        raise ValuationUnavailableError(msg)
    return price
