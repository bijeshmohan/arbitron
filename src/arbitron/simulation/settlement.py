"""How a fill changes balances: one rule shared by the paper venue and backtests."""

from decimal import Decimal, localcontext

from arbitron.domain.enums import Side
from arbitron.domain.money import FINANCIAL_CONTEXT


def settlement_deltas(
    *, side: Side, filled: Decimal, price: Decimal, fee: Decimal
) -> tuple[Decimal, Decimal]:
    """(base change, quote change) for a fill whose fee is in the received asset.

    BUY: pay ``filled x price`` quote, receive ``filled - fee`` base.
    SELL: give ``filled`` base, receive ``filled x price - fee`` quote.
    """
    with localcontext(FINANCIAL_CONTEXT):
        notional = filled * price
        if side is Side.BUY:
            return filled - fee, -notional
        return -filled, notional - fee
