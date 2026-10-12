"""Position accounting from fills: weighted average cost (plan §13).

- BUY: quantity grows by what was actually received (the filled amount minus
  any fee charged in the base asset); cost basis grows by the quote spent
  (plus any fee charged in quote). Average cost = cost basis / quantity, so
  base-asset fees raise the effective average cost.
- SELL: quantity shrinks by the amount sold (plus any fee charged in base);
  realized P&L += proceeds (minus any quote fee) - average cost x base removed.
  Average cost is unchanged by a sale, and resets to 0 when flat.
- Fees in any other currency (e.g. BNB discounts) are not supported and fail
  closed rather than being silently ignored.

Average cost and realized P&L are quantized to 18 decimal places (the
database's NUMERIC scale) so that a reloaded position equals the in-memory one.
"""

from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from arbitron.domain.enums import Side
from arbitron.domain.fills import Fill
from arbitron.domain.market import Market
from arbitron.domain.money import FINANCIAL_CONTEXT
from arbitron.domain.positions import Position

_SCALE = Decimal("1E-18")


class AccountingError(Exception):
    """A fill cannot be applied (unsupported fee currency, overselling, wrong market)."""


def _q(value: Decimal) -> Decimal:
    return value.quantize(_SCALE, rounding=ROUND_HALF_EVEN)


def apply_fill(position: Position | None, fill: Fill, market: Market) -> Position:
    trade = fill.trade
    if trade.symbol != market.symbol or trade.exchange_id != market.exchange_id:
        msg = f"fill for {trade.exchange_id}:{trade.symbol} applied to {market.symbol}"
        raise AccountingError(msg)
    current = position or Position(
        exchange_id=market.exchange_id,
        symbol=market.symbol,
        quantity=Decimal(0),
        average_cost=Decimal(0),
        realized_pnl=Decimal(0),
        updated_at=trade.timestamp,
    )
    fee_base = fee_quote = Decimal(0)
    if trade.fee is not None and trade.fee.amount:
        if trade.fee.currency == market.base:
            fee_base = trade.fee.amount
        elif trade.fee.currency == market.quote:
            fee_quote = trade.fee.amount
        else:
            msg = f"fee in {trade.fee.currency} is not supported for {market.symbol}"
            raise AccountingError(msg)

    with localcontext(FINANCIAL_CONTEXT):
        notional = trade.amount * trade.price
        fee_value = fee_quote + fee_base * trade.price
        if trade.side is Side.BUY:
            received = trade.amount - fee_base
            quantity = current.quantity + received
            basis = current.quantity * current.average_cost + notional + fee_quote
            average = _q(basis / quantity) if quantity else Decimal(0)
            realized = current.realized_pnl
        else:
            removed = trade.amount + fee_base
            if removed > current.quantity:
                msg = (
                    f"sell of {removed} {market.base} exceeds the tracked position "
                    f"of {current.quantity}"
                )
                raise AccountingError(msg)
            quantity = current.quantity - removed
            proceeds = notional - fee_quote
            realized = _q(current.realized_pnl + proceeds - current.average_cost * removed)
            average = current.average_cost if quantity else Decimal(0)
        return Position(
            exchange_id=current.exchange_id,
            symbol=current.symbol,
            quantity=quantity,
            average_cost=average,
            realized_pnl=realized,
            updated_at=max(current.updated_at, trade.timestamp),
            fees_paid=_q(current.fees_paid + fee_value),
        )
