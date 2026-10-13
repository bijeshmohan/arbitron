"""Synthetic quotes for backtests, where no order book exists.

A quote is built around a reference price (a candle's open or close) with a
full spread of ``spread_bps``: bid = price x (1 - half spread), rounded down
to the tick; ask = price x (1 + half spread), rounded up. Rounding is always
against the trader, and the ask is at least one tick above the bid.
"""

from datetime import datetime
from decimal import Decimal, localcontext

from arbitron.domain.market import Market, Ticker
from arbitron.domain.money import FINANCIAL_CONTEXT, round_down_to_step, round_up_to_step

_BPS = Decimal(10_000)


def synthetic_quote(
    *, price: Decimal, spread_bps: Decimal, market: Market, timestamp: datetime
) -> Ticker:
    if spread_bps < 0:
        msg = "spread_bps must not be negative"
        raise ValueError(msg)
    with localcontext(FINANCIAL_CONTEXT):
        half = spread_bps / _BPS / 2
        bid = round_down_to_step(price * (1 - half), market.price_step)
        ask = round_up_to_step(price * (1 + half), market.price_step)
        if ask <= bid:
            ask = bid + market.price_step
    return Ticker(market.exchange_id, market.symbol, bid, ask, price, timestamp)
