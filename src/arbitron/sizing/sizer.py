"""Signal -> sized order intent (plan §11.2), policy ``fixed_quote_notional_v1``.

Operator-chosen rules:

- **Sizing:** each entry spends a fixed quote amount (``entry_notional``).
- **Positions:** BUY opens a position only when flat (no adding); SELL closes
  the whole position. Holdings too small to trade (below the venue minimums)
  count as flat: they cannot be sold, so they must not block re-entry.
- **Pricing:** marketable limit orders, immediate-or-cancel. BUY limit is the
  best ask plus ``max_slippage_pct``, rounded down to the price tick; SELL
  limit is the best bid minus ``max_slippage_pct``, rounded up. An order thus
  fills now within the cap or is cancelled; nothing rests on the book.

The sizer proposes; it never approves. Every intent still goes through the
risk engine, which re-verifies all of this independently. "No intent" is a
normal outcome and carries a reason.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum

from arbitron.domain.enums import OrderType, Side, TimeInForce
from arbitron.domain.market import Market
from arbitron.domain.money import (
    FINANCIAL_CONTEXT,
    canonical_str,
    round_down_to_step,
    round_up_to_step,
)
from arbitron.domain.orders import OrderIntent
from arbitron.domain.signals import Signal
from arbitron.domain.snapshot import TradingSnapshot

POLICY_NAME = "fixed_quote_notional_v1"
_HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class SizingPolicy:
    entry_notional: Decimal  # quote spent per entry
    max_slippage_pct: Decimal  # price cap/floor distance from best ask/bid
    max_ticker_age: timedelta


class NoIntentReason(StrEnum):
    SIZED = "sized"
    MARKET_UNAVAILABLE = "market_unavailable"
    TICKER_UNAVAILABLE = "ticker_unavailable"
    ALREADY_IN_POSITION = "already_in_position"
    ENTRY_PENDING = "entry_pending"
    NO_POSITION = "no_position"
    EXIT_PENDING = "exit_pending"
    BELOW_MINIMUM = "below_minimum"
    BAND_NARROWER_THAN_TICK = "band_narrower_than_tick"


@dataclass(frozen=True, slots=True)
class SizingResult:
    intent: OrderIntent | None
    reason: NoIntentReason
    detail: str = ""


def size(
    signal: Signal,
    snapshot: TradingSnapshot,
    policy: SizingPolicy,
    *,
    now: datetime,
    attempt: int = 1,
) -> SizingResult:
    market = snapshot.markets.get(signal.symbol)
    if market is None or not market.active:
        return SizingResult(None, NoIntentReason.MARKET_UNAVAILABLE, signal.symbol)
    ticker = snapshot.tickers.get(signal.symbol)
    if (
        ticker is None
        or ticker.bid is None
        or ticker.ask is None
        or now - ticker.timestamp > policy.max_ticker_age
    ):
        return SizingResult(None, NoIntentReason.TICKER_UNAVAILABLE, "no fresh bid/ask")
    if signal.side is Side.BUY:
        return _size_entry(
            signal=signal,
            snapshot=snapshot,
            policy=policy,
            market=market,
            ask=ticker.ask,
            now=now,
            attempt=attempt,
        )
    return _size_exit(
        signal=signal,
        snapshot=snapshot,
        policy=policy,
        market=market,
        bid=ticker.bid,
        now=now,
        attempt=attempt,
    )


def _size_entry(
    *,
    signal: Signal,
    snapshot: TradingSnapshot,
    policy: SizingPolicy,
    market: Market,
    ask: Decimal,
    now: datetime,
    attempt: int,
) -> SizingResult:
    held = market.tradeable_quantity(snapshot.held_quantity(signal.symbol), ask)
    if held > 0:
        return SizingResult(None, NoIntentReason.ALREADY_IN_POSITION, f"holding {held}")
    if any(o.intent.side is Side.BUY for o in snapshot.open_orders_for(signal.symbol)):
        return SizingResult(None, NoIntentReason.ENTRY_PENDING, "an entry order is open")

    with localcontext(FINANCIAL_CONTEXT):
        cap = ask * (1 + policy.max_slippage_pct / _HUNDRED)
        limit = round_down_to_step(cap, market.price_step)
        if limit < ask:
            return SizingResult(
                None,
                NoIntentReason.BAND_NARROWER_THAN_TICK,
                f"slippage cap {cap} rounds below ask {ask}",
            )
        amount = round_down_to_step(policy.entry_notional / limit, market.amount_step)
        notional = amount * limit
    if amount == 0 or amount < market.min_amount or notional < market.min_cost:
        return SizingResult(
            None,
            NoIntentReason.BELOW_MINIMUM,
            f"{amount} @ {limit} = {notional} is below the venue minimums",
        )
    return SizingResult(
        _intent(
            signal,
            Side.BUY,
            amount=amount,
            limit=limit,
            now=now,
            attempt=attempt,
            policy=policy,
            ask=ask,
            cap=cap,
        ),
        NoIntentReason.SIZED,
    )


def _size_exit(
    *,
    signal: Signal,
    snapshot: TradingSnapshot,
    policy: SizingPolicy,
    market: Market,
    bid: Decimal,
    now: datetime,
    attempt: int,
) -> SizingResult:
    held = snapshot.held_quantity(signal.symbol)
    if market.tradeable_quantity(held, bid) == 0:
        return SizingResult(None, NoIntentReason.NO_POSITION, f"holding {held}")
    base = snapshot.base_balance(signal.symbol)
    free = base.free if base is not None else Decimal(0)
    sellable = min(held - snapshot.committed_sells(signal.symbol), free)

    with localcontext(FINANCIAL_CONTEXT):
        floor = bid * (1 - policy.max_slippage_pct / _HUNDRED)
        limit = round_up_to_step(floor, market.price_step)
    if limit > bid:
        return SizingResult(
            None,
            NoIntentReason.BAND_NARROWER_THAN_TICK,
            f"slippage floor {floor} rounds above bid {bid}",
        )
    amount = market.tradeable_quantity(sellable, limit)
    if amount == 0:
        return SizingResult(None, NoIntentReason.EXIT_PENDING, "position already being sold")
    return SizingResult(
        _intent(
            signal,
            Side.SELL,
            amount=amount,
            limit=limit,
            now=now,
            attempt=attempt,
            policy=policy,
            bid=bid,
            floor=floor,
        ),
        NoIntentReason.SIZED,
    )


def _intent(
    signal: Signal,
    side: Side,
    *,
    amount: Decimal,
    limit: Decimal,
    now: datetime,
    attempt: int,
    policy: SizingPolicy,
    **prices: Decimal,
) -> OrderIntent:
    inputs = {name: canonical_str(value) for name, value in prices.items()}
    inputs["entry_notional"] = canonical_str(policy.entry_notional)
    inputs["max_slippage_pct"] = canonical_str(policy.max_slippage_pct)
    return OrderIntent(
        signal_id=signal.id,
        strategy_id=signal.strategy_id,
        exchange_id=signal.exchange_id,
        symbol=signal.symbol,
        side=side,
        order_type=OrderType.LIMIT,
        amount=amount,
        limit_price=limit,
        time_in_force=TimeInForce.IOC,
        created_at=now,
        sizing_policy=POLICY_NAME,
        sizing_inputs=inputs,
        attempt=attempt,
    )
