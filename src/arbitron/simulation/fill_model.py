"""Paper / backtest fill model (plan §17.2): top of book plus slippage.

Operator-chosen model (2026-10-12), applied to limit orders:

1. **Reference price.** BUY: best ask. SELL: best bid. From a ticker fetched
   at submission time.
2. **Slippage.** The fill price is the reference moved against the order by
   ``slippage_bps`` basis points, then rounded to the price tick against the
   order (BUY up, SELL down). This approximates impact and latency, and is
   slightly pessimistic.
3. **Limit.** If that price breaches the order's limit (BUY above, SELL
   below), an IOC order **expires unfilled**. Otherwise it fills.
4. **Quantity.** The whole order fills (``fill_ratio`` = 1). A lower ratio is
   available to exercise partial-fill handling in tests; the remainder of an
   IOC order then expires. The filled quantity is rounded down to the lot step.
5. **Fees.** ``fee_rate`` of the received asset, like Binance spot by
   default: BUY pays in base (you receive ``amount x (1 - fee_rate)``), SELL
   pays in quote. Fees are rounded half-even to 18 decimal places.

Only IOC limit orders are modeled; this build creates nothing else. The model
is pure and deterministic: the same order and ticker always give the same fill.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from arbitron.domain.enums import OrderType, Side, TimeInForce
from arbitron.domain.market import Market, Ticker
from arbitron.domain.money import (
    FINANCIAL_CONTEXT,
    round_down_to_step,
    round_up_to_step,
)

_BPS = Decimal(10_000)
_FEE_SCALE = Decimal("1E-18")  # the database's NUMERIC scale


class UnsupportedOrderError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    filled: Decimal  # 0 when the order expired unfilled
    price: Decimal | None
    fee: Decimal
    fee_currency: str

    @property
    def is_fill(self) -> bool:
        return self.filled > 0


@dataclass(frozen=True, slots=True)
class FillModel:
    slippage_bps: Decimal
    fee_rate: Decimal  # 0.001 = 0.1%
    fill_ratio: Decimal = Decimal(1)

    def __post_init__(self) -> None:
        if self.slippage_bps < 0 or self.fee_rate < 0:
            msg = "slippage and fee must not be negative"
            raise ValueError(msg)
        if not 0 < self.fill_ratio <= 1:
            msg = "fill_ratio must be in (0, 1]"
            raise ValueError(msg)

    def fill(
        self,
        *,
        side: Side,
        order_type: OrderType,
        time_in_force: TimeInForce | None,
        amount: Decimal,
        limit_price: Decimal | None,
        ticker: Ticker,
        market: Market,
    ) -> SimulatedFill:
        if order_type is not OrderType.LIMIT or time_in_force is not TimeInForce.IOC:
            msg = "the paper fill model only supports IOC limit orders"
            raise UnsupportedOrderError(msg)
        if limit_price is None:  # pragma: no cover - LIMIT intents always carry a price
            msg = "limit order without a limit price"
            raise UnsupportedOrderError(msg)
        fee_currency = market.base if side is Side.BUY else market.quote
        reference = ticker.ask if side is Side.BUY else ticker.bid
        if reference is None:
            return SimulatedFill(Decimal(0), None, Decimal(0), fee_currency)

        with localcontext(FINANCIAL_CONTEXT):
            if side is Side.BUY:
                price = round_up_to_step(
                    reference * (1 + self.slippage_bps / _BPS), market.price_step
                )
                breaches = price > limit_price
            else:
                price = round_down_to_step(
                    reference * (1 - self.slippage_bps / _BPS), market.price_step
                )
                breaches = price < limit_price
            if breaches:
                return SimulatedFill(Decimal(0), None, Decimal(0), fee_currency)
            filled = round_down_to_step(amount * self.fill_ratio, market.amount_step)
            if filled == 0:
                return SimulatedFill(Decimal(0), None, Decimal(0), fee_currency)
            fee = ((filled if side is Side.BUY else filled * price) * self.fee_rate).quantize(
                _FEE_SCALE, rounding=ROUND_HALF_EVEN
            )
        return SimulatedFill(filled, price, fee, fee_currency)
