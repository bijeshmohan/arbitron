"""The risk gate (plan §11): approve or reject a sized intent, with reasons.

Deterministic and pure: the same intent, snapshot, and policy always give the
same decision. Every applicable reason is collected (not just the first), so
a rejection explains everything that was wrong.

Which checks apply to which orders:

- **Fail-closed conditions block every order, exits included** (§11.4):
  kill switch not armed, trading disabled, system not ready, reconciliation
  unresolved, exchange unhealthy, market data not ok, unknown or inactive
  market, missing/stale ticker, and precision/minimum/price-band violations.
- **Risk-increasing limits block BUYs only**: per-order notional, exposure,
  open-order count, order frequency, daily loss, drawdown, balance and
  reserve, and the enter-only-when-flat rule. Blocking an exit when limits
  are breached would *increase* risk, so exits are exempt from them.

``decide`` never raises: if evaluation itself fails, the intent is rejected
with ``RISK_STATE_UNAVAILABLE``.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal, localcontext

from arbitron.domain.enums import OrderType, Side
from arbitron.domain.market import Market
from arbitron.domain.money import FINANCIAL_CONTEXT, canonical_str, is_multiple_of_step
from arbitron.domain.orders import OrderIntent
from arbitron.domain.risk import RiskDecision, RiskReason
from arbitron.domain.snapshot import TradingSnapshot
from arbitron.risk.exposure import (
    ValuationUnavailableError,
    asset_exposure,
    committed_quote,
    total_exposure,
    valuation,
)

R = RiskReason
_HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    version: str  # changes whenever any limit changes
    allowed_symbols: frozenset[str]
    max_order_notional: Decimal
    max_asset_exposure: Decimal
    max_total_exposure: Decimal
    max_open_orders: int
    max_orders_per_hour: int
    max_daily_loss: Decimal
    max_drawdown_pct: Decimal
    max_spread_pct: Decimal
    max_slippage_pct: Decimal  # price band the limit price must sit within
    max_ticker_age: timedelta
    min_quote_reserve: Decimal
    fee_rate: Decimal  # assumed fee as a fraction (0.001 = 0.1%)
    allow_market_orders: bool


class _Checks:
    def __init__(self) -> None:
        self.reasons: list[RiskReason] = []
        self.observations: dict[str, str] = {}

    def fail(self, reason: RiskReason, **observed: Decimal | int | str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)
        self.note(**observed)

    def note(self, **observed: Decimal | int | str) -> None:
        for key, value in observed.items():
            self.observations[key] = (
                canonical_str(value) if isinstance(value, Decimal) else str(value)
            )


def decide(
    intent: OrderIntent, snapshot: TradingSnapshot, policy: RiskPolicy, *, now: datetime
) -> RiskDecision:
    """Evaluate ``intent``. Never raises; internal failure rejects (fail closed)."""
    try:
        checks = _evaluate(intent, snapshot, policy)
    except Exception as exc:  # any failure must reject, never approve
        checks = _Checks()
        checks.fail(R.RISK_STATE_UNAVAILABLE, error=f"{type(exc).__name__}: {exc}")
    return RiskDecision(
        intent_id=intent.id,
        reasons=tuple(checks.reasons),
        policy_version=policy.version,
        decided_at=now,
        observations=checks.observations,
    )


def _evaluate(intent: OrderIntent, snapshot: TradingSnapshot, policy: RiskPolicy) -> _Checks:
    checks = _Checks()
    # The intent under evaluation may already be persisted as an open order;
    # it must not count against itself.
    snapshot = replace(
        snapshot, open_orders=tuple(o for o in snapshot.open_orders if o.id != intent.id)
    )
    _system(checks, intent, snapshot)
    market = snapshot.markets.get(intent.symbol)
    if intent.symbol not in policy.allowed_symbols or market is None or not market.active:
        checks.fail(R.MARKET_NOT_ALLOWED, symbol=intent.symbol)
        return checks
    if intent.order_type is OrderType.MARKET or intent.limit_price is None:
        checks.fail(R.MARKET_ORDERS_DISALLOWED, order_type=intent.order_type.value)
        return checks  # without a limit price nothing below can be bounded

    limit = intent.limit_price
    with localcontext(FINANCIAL_CONTEXT):
        notional = intent.amount * limit
    checks.note(amount=intent.amount, limit_price=limit, notional=notional, side=intent.side.value)
    _precision(checks, intent, market, limit, notional)
    _price_band(checks, intent, snapshot, policy, limit)
    if intent.side is Side.BUY:
        _entry_limits(
            checks,
            intent,
            snapshot=snapshot,
            policy=policy,
            market=market,
            limit=limit,
            notional=notional,
        )
    else:
        _exit_limits(checks, intent, snapshot)
    return checks


def _system(checks: _Checks, intent: OrderIntent, snapshot: TradingSnapshot) -> None:
    kill_switch = snapshot.kill_switch
    if kill_switch is None or not kill_switch.allows_new_orders:
        checks.fail(
            R.KILL_SWITCH_NOT_ARMED,
            kill_switch=kill_switch.state.value if kill_switch else "uninitialized",
        )
    if not snapshot.trading_enabled:
        checks.fail(R.TRADING_DISABLED)
    if not snapshot.system_ready:
        checks.fail(R.SYSTEM_NOT_READY)
    if not snapshot.reconciliation_clean:
        checks.fail(R.RECONCILIATION_UNRESOLVED)
    if not snapshot.exchange_healthy:
        checks.fail(R.EXCHANGE_ERROR_RATE)
    if snapshot.market_data_ok.get(intent.symbol) is not True:
        checks.fail(R.MARKET_DATA_STALE)


def _precision(
    checks: _Checks, intent: OrderIntent, market: Market, limit: Decimal, notional: Decimal
) -> None:
    if not is_multiple_of_step(intent.amount, market.amount_step) or not is_multiple_of_step(
        limit, market.price_step
    ):
        checks.fail(
            R.PRECISION_VIOLATION, amount_step=market.amount_step, price_step=market.price_step
        )
    if intent.amount < market.min_amount or notional < market.min_cost:
        checks.fail(R.BELOW_MINIMUM, min_amount=market.min_amount, min_cost=market.min_cost)


def _price_band(
    checks: _Checks,
    intent: OrderIntent,
    snapshot: TradingSnapshot,
    policy: RiskPolicy,
    limit: Decimal,
) -> None:
    ticker = snapshot.tickers.get(intent.symbol)
    if (
        ticker is None
        or ticker.bid is None
        or ticker.ask is None
        or snapshot.as_of - ticker.timestamp > policy.max_ticker_age
    ):
        checks.fail(R.TICKER_UNAVAILABLE)
        return
    with localcontext(FINANCIAL_CONTEXT):
        mid = (ticker.bid + ticker.ask) / 2
        spread_pct = (ticker.ask - ticker.bid) / mid * _HUNDRED
        slip = policy.max_slippage_pct / _HUNDRED
        cap, floor = ticker.ask * (1 + slip), ticker.bid * (1 - slip)
    checks.note(bid=ticker.bid, ask=ticker.ask, spread_pct=spread_pct)
    if spread_pct > policy.max_spread_pct:
        checks.fail(R.SPREAD_TOO_WIDE, max_spread_pct=policy.max_spread_pct)
    if (intent.side is Side.BUY and limit > cap) or (intent.side is Side.SELL and limit < floor):
        checks.fail(R.PRICE_OUTSIDE_BAND, price_cap=cap, price_floor=floor)


def _entry_limits(
    checks: _Checks,
    intent: OrderIntent,
    *,
    snapshot: TradingSnapshot,
    policy: RiskPolicy,
    market: Market,
    limit: Decimal,
    notional: Decimal,
) -> None:
    if notional > policy.max_order_notional:
        checks.fail(R.MAX_ORDER_NOTIONAL, max_order_notional=policy.max_order_notional)

    # Enter only when flat: any tradeable holding or open entry blocks a BUY.
    # Same "tradeable" rule as the sizer: dust below venue minimums counts as flat.
    held = market.tradeable_quantity(snapshot.held_quantity(intent.symbol), limit)
    entry_open = any(o.intent.side is Side.BUY for o in snapshot.open_orders_for(intent.symbol))
    if held > 0 or entry_open:
        checks.fail(R.POSITION_POLICY, held=held, entry_open=str(entry_open))

    open_count = len(snapshot.open_orders) + 1
    if open_count > policy.max_open_orders:
        checks.fail(
            R.MAX_OPEN_ORDERS, open_orders_after=open_count, max_open_orders=policy.max_open_orders
        )
    if snapshot.orders_last_hour >= policy.max_orders_per_hour:
        checks.fail(
            R.ORDER_FREQUENCY,
            orders_last_hour=snapshot.orders_last_hour,
            max_orders_per_hour=policy.max_orders_per_hour,
        )

    try:
        with localcontext(FINANCIAL_CONTEXT):
            asset_after = asset_exposure(snapshot, intent.symbol) + notional
            total_after = total_exposure(snapshot) + notional
            equity = valuation(snapshot).equity
    except ValuationUnavailableError as exc:
        checks.fail(R.VALUATION_UNAVAILABLE, valuation_error=str(exc))
    else:
        checks.note(
            asset_exposure_after=asset_after, total_exposure_after=total_after, equity=equity
        )
        if asset_after > policy.max_asset_exposure:
            checks.fail(R.MAX_ASSET_EXPOSURE, max_asset_exposure=policy.max_asset_exposure)
        if total_after > policy.max_total_exposure:
            checks.fail(R.MAX_TOTAL_EXPOSURE, max_total_exposure=policy.max_total_exposure)
        _loss_limits(checks, snapshot, policy, equity)
    _balance(checks, snapshot, policy, notional)


def _loss_limits(
    checks: _Checks, snapshot: TradingSnapshot, policy: RiskPolicy, equity: Decimal
) -> None:
    for reason in loss_limit_breaches(snapshot, policy, equity, checks):
        checks.fail(reason)


def loss_limit_breaches(
    snapshot: TradingSnapshot,
    policy: RiskPolicy,
    equity: Decimal,
    checks: _Checks | None = None,
) -> tuple[RiskReason, ...]:
    """Daily-loss and drawdown breaches (also used to decide kill-switch trips)."""
    breaches: list[RiskReason] = []
    note = checks.note if checks is not None else _ignore
    with localcontext(FINANCIAL_CONTEXT):
        if snapshot.day_start_equity is None:
            breaches.append(R.VALUATION_UNAVAILABLE)
            note(day_start_equity="unknown")
        else:
            daily_loss = snapshot.day_start_equity - equity
            note(daily_loss=daily_loss, max_daily_loss=policy.max_daily_loss)
            if daily_loss >= policy.max_daily_loss:
                breaches.append(R.MAX_DAILY_LOSS)
        if not snapshot.peak_equity:
            breaches.append(R.VALUATION_UNAVAILABLE)
            note(peak_equity="unknown")
        else:
            drawdown_pct = (snapshot.peak_equity - equity) / snapshot.peak_equity * _HUNDRED
            note(drawdown_pct=drawdown_pct, max_drawdown_pct=policy.max_drawdown_pct)
            if drawdown_pct >= policy.max_drawdown_pct:
                breaches.append(R.MAX_DRAWDOWN)
    return tuple(dict.fromkeys(breaches))


def _ignore(**_observed: Decimal | int | str) -> None:
    return None


def _balance(
    checks: _Checks, snapshot: TradingSnapshot, policy: RiskPolicy, notional: Decimal
) -> None:
    quote = snapshot.quote_balance()
    with localcontext(FINANCIAL_CONTEXT):
        available = (quote.free if quote is not None else Decimal(0)) - committed_quote(
            snapshot, policy.fee_rate
        )
        cost = notional * (1 + policy.fee_rate)
        remaining = available - cost
    checks.note(available_quote=available, cost_with_fees=cost, quote_after=remaining)
    if cost > available:
        checks.fail(R.INSUFFICIENT_BALANCE)
    elif remaining < policy.min_quote_reserve:
        checks.fail(R.BALANCE_RESERVE, min_quote_reserve=policy.min_quote_reserve)


def _exit_limits(checks: _Checks, intent: OrderIntent, snapshot: TradingSnapshot) -> None:
    base = snapshot.base_balance(intent.symbol)
    free = base.free if base is not None else Decimal(0)
    sellable = min(
        snapshot.held_quantity(intent.symbol) - snapshot.committed_sells(intent.symbol), free
    )
    checks.note(sellable=sellable)
    if intent.amount > sellable:
        checks.fail(R.INSUFFICIENT_POSITION)
