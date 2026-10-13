"""Deterministic backtest engine (plan §17.1).

Pure: candles, markets, strategy, and policies in; a result out. No clock,
no I/O, no randomness, so identical inputs always produce identical results.

It reuses the bot's own decision code: the same strategy evaluation, sizer,
risk gate, fill model, settlement rule, and position accounting as paper
trading. Operator-chosen modeling (2026-10-13):

- **Timing / no lookahead.** At each candle's *close*: mark equity, check
  loss limits, evaluate the strategy on candles up to and including that
  candle, then size and risk-check any signal. An approved order fills at
  the **next candle's open**, the earliest price tradable after the
  decision. If the next candle is missing (an outage gap), the order
  expires unfilled.
- **Prices.** Decisions see a synthetic quote around the close; fills use a
  synthetic quote around the next open (``spread_bps``), then the shared
  fill model applies ``slippage_bps`` and the IOC limit. An open that gaps
  past the limit means the order expires.
- **Risk.** The full risk gate applies. A daily-loss or drawdown breach trips
  the kill switch and **halts trading for the rest of the run** (no operator
  re-arms it); later signals are still evaluated and rejected.
- **Warm-up.** Candles before ``start`` only provide strategy history; no
  decisions or equity are recorded before ``start``.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from types import MappingProxyType

from arbitron.domain.enums import KillSwitchState, OrderStatus, Side
from arbitron.domain.fills import ExchangeTrade, Fee, Fill
from arbitron.domain.market import Candle, Market, Ticker, Timeframe
from arbitron.domain.money import FINANCIAL_CONTEXT
from arbitron.domain.orders import Order, OrderIntent
from arbitron.domain.portfolio import Balance, PortfolioView
from arbitron.domain.positions import Position
from arbitron.domain.risk import KillSwitch, RiskReason
from arbitron.domain.snapshot import TradingSnapshot
from arbitron.portfolio.accounting import apply_fill
from arbitron.risk.exposure import valuation
from arbitron.risk.manager import RiskPolicy, decide, loss_limit_breaches
from arbitron.simulation.fill_model import FillModel, SimulatedFill
from arbitron.simulation.quotes import synthetic_quote
from arbitron.simulation.settlement import settlement_deltas
from arbitron.sizing.sizer import SizingPolicy, size
from arbitron.strategy.base import Strategy
from arbitron.strategy.evaluation import evaluate_candle, window_problem

ACTOR = "backtest"
TRIP_REASONS = frozenset({RiskReason.MAX_DAILY_LOSS, RiskReason.MAX_DRAWDOWN})


@dataclass(frozen=True, slots=True)
class Assumptions:
    starting_balance: Decimal
    spread_bps: Decimal
    slippage_bps: Decimal
    fee_rate: Decimal  # 0.001 = 0.1%

    def describe(self) -> dict[str, str]:
        return {
            "fill_timing": "next candle open",
            "starting_balance": str(self.starting_balance),
            "spread_bps": str(self.spread_bps),
            "slippage_bps": str(self.slippage_bps),
            "fee_pct": str(self.fee_rate * 100),
            "fee_currency": "received asset",
            "order_type": "IOC limit (expires if the next open breaches the limit)",
            "kill_switch": "loss-limit breach halts trading for the rest of the run",
        }


@dataclass(frozen=True, slots=True)
class Inputs:
    strategy: Strategy
    sizing: SizingPolicy
    risk: RiskPolicy
    markets: Mapping[str, Market]
    candles: Mapping[str, Sequence[Candle]]  # per symbol, closed, ascending
    timeframe: Timeframe
    quote_currency: str
    trading_enabled: bool
    start: datetime  # first candle open time that may produce decisions


@dataclass(frozen=True, slots=True)
class TradeRecord:
    time: datetime
    symbol: str
    side: Side
    amount: Decimal
    price: Decimal
    fee: Decimal
    fee_currency: str
    realized_pnl_change: Decimal


@dataclass(frozen=True, slots=True)
class EquityPoint:
    time: datetime  # candle close
    equity: Decimal
    exposure: Decimal  # value held in base assets (including dust)
    # Holding a tradeable position (dust below venue minimums counts as flat,
    # the same rule the sizer and risk gate use).
    in_market: bool = False


@dataclass
class Result:
    equity: list[EquityPoint] = field(default_factory=list[EquityPoint])
    trades: list[TradeRecord] = field(default_factory=list[TradeRecord])
    signals: int = 0
    approved: int = 0
    rejected: dict[str, int] = field(default_factory=dict[str, int])
    not_sized: dict[str, int] = field(default_factory=dict[str, int])
    expired: int = 0  # approved but unfilled (limit breached or no next candle)
    skipped_windows: int = 0  # candles without a complete lookback window
    halted_at: datetime | None = None
    halt_reason: str | None = None
    final_balances: dict[str, Decimal] = field(default_factory=dict[str, Decimal])
    final_positions: dict[str, Position] = field(default_factory=dict[str, Position])


@dataclass
class _State:
    balances: dict[str, Decimal]
    positions: dict[str, Position] = field(default_factory=dict[str, Position])
    pending: list[Order] = field(default_factory=list[Order])
    intents_created: list[datetime] = field(default_factory=list[datetime])
    kill_switch: KillSwitch | None = None


def run(inputs: Inputs, assumptions: Assumptions) -> Result:
    return _Run(inputs, assumptions).execute()


class _Run:
    def __init__(self, inputs: Inputs, assumptions: Assumptions) -> None:
        self.inputs = inputs
        self.assumptions = assumptions
        self.duration = inputs.timeframe.duration
        self.by_symbol = {s: {c.open_time: c for c in cs} for s, cs in inputs.candles.items()}
        self.model = FillModel(slippage_bps=assumptions.slippage_bps, fee_rate=assumptions.fee_rate)
        self.result = Result()
        self.state = _State(
            balances={inputs.quote_currency: assumptions.starting_balance},
            kill_switch=KillSwitch(KillSwitchState.ARMED, inputs.start, ACTOR, "backtest start"),
        )
        self.last_close: dict[str, Decimal] = {}

    def execute(self) -> Result:
        timeline = sorted(
            {t for cs in self.by_symbol.values() for t in cs if t >= self.inputs.start}
        )
        for open_time in timeline:
            close_time = open_time + self.duration
            # 1. Orders decided at the previous close fill at this candle's open.
            self._fill_pending(open_time)
            for symbol, candles in self.by_symbol.items():
                candle = candles.get(open_time)
                if candle is not None:
                    self.last_close[symbol] = candle.close
            # 2. At this candle's close: equity, loss limits, then decisions.
            snapshot = self._snapshot(close_time)
            equity = self._record_equity(snapshot, close_time)
            self._enforce_loss_limits(snapshot, equity, close_time)
            for symbol, candles in self.by_symbol.items():
                if open_time in candles:
                    self._decide(symbol, open_time, close_time)
        self.result.expired += len(self.state.pending)  # no next candle to fill them
        self.state.pending = []
        self.result.final_balances = dict(self.state.balances)
        self.result.final_positions = dict(self.state.positions)
        return self.result

    def _fill_pending(self, open_time: datetime) -> None:
        pending, self.state.pending = self.state.pending, []
        for order in pending:
            intent = order.intent
            # An order may only fill at the open immediately after its decision.
            # If that candle is missing (outage gap), it expires: filling at a
            # later candle would use a price the decision never saw.
            fill_at = order.updated_at  # decision close == next candle's open
            candle = self.by_symbol[intent.symbol].get(fill_at)
            if open_time < fill_at:
                self.state.pending.append(order)  # its candle is still ahead
                continue
            market = self.inputs.markets[intent.symbol]
            if candle is None or not self._affordable(intent, market):
                self.result.expired += 1  # outage gap, or funds moved by an earlier fill
                continue
            quote = synthetic_quote(
                price=candle.open,
                spread_bps=self.assumptions.spread_bps,
                market=market,
                timestamp=open_time,
            )
            fill = self.model.fill(
                side=intent.side,
                order_type=intent.order_type,
                time_in_force=intent.time_in_force,
                amount=intent.amount,
                limit_price=intent.limit_price,
                ticker=quote,
                market=market,
            )
            if not fill.is_fill or fill.price is None:
                self.result.expired += 1
                continue
            self._settle(order, market, fill, fill_at)

    def _settle(self, order: Order, market: Market, fill: SimulatedFill, at: datetime) -> None:
        intent = order.intent
        assert fill.price is not None  # noqa: S101 - only called for real fills
        filled, price, fee, fee_currency = fill.filled, fill.price, fill.fee, fill.fee_currency
        base_delta, quote_delta = settlement_deltas(
            side=intent.side, filled=filled, price=price, fee=fee
        )
        balances = self.state.balances
        with localcontext(FINANCIAL_CONTEXT):
            balances[market.base] = balances.get(market.base, Decimal(0)) + base_delta
            balances[market.quote] = balances.get(market.quote, Decimal(0)) + quote_delta
        trade = ExchangeTrade(
            exchange_id=market.exchange_id,
            trade_id=f"bt-{intent.id}",
            exchange_order_id=f"bt-{intent.id}",
            symbol=intent.symbol,
            side=intent.side,
            amount=filled,
            price=price,
            fee=Fee(fee, fee_currency),
            timestamp=at,
        )
        previous = self.state.positions.get(intent.symbol)
        position = apply_fill(previous, Fill(order.id, trade), market)
        self.state.positions[intent.symbol] = position
        realized_before = previous.realized_pnl if previous is not None else Decimal(0)
        self.result.trades.append(
            TradeRecord(
                time=at,
                symbol=intent.symbol,
                side=intent.side,
                amount=filled,
                price=price,
                fee=fee,
                fee_currency=fee_currency,
                realized_pnl_change=position.realized_pnl - realized_before,
            )
        )

    def _affordable(self, intent: OrderIntent, market: Market) -> bool:
        """The venue's own funds check (as the paper venue does), at the limit price."""
        balances = self.state.balances
        with localcontext(FINANCIAL_CONTEXT):
            if intent.side is Side.BUY:
                needed = intent.amount * (intent.limit_price or Decimal(0))
                return balances.get(market.quote, Decimal(0)) >= needed
            return balances.get(market.base, Decimal(0)) >= intent.amount

    def _snapshot(self, close_time: datetime) -> TradingSnapshot:
        tickers: dict[str, Ticker] = {
            symbol: synthetic_quote(
                price=price,
                spread_bps=self.assumptions.spread_bps,
                market=self.inputs.markets[symbol],
                timestamp=close_time,
            )
            for symbol, price in self.last_close.items()
        }
        balances = {
            asset: Balance(asset, amount, Decimal(0), amount, close_time)
            for asset, amount in self.state.balances.items()
        }
        hour_ago = close_time - timedelta(hours=1)
        return TradingSnapshot(
            as_of=close_time,
            quote_currency=self.inputs.quote_currency,
            markets=dict(self.inputs.markets),
            marks=dict(self.last_close),
            tickers=tickers,
            balances=balances,
            open_orders=tuple(self.state.pending),
            orders_last_hour=sum(1 for t in self.state.intents_created if t > hour_ago),
            day_start_equity=None,
            peak_equity=None,
            market_data_ok={},
            kill_switch=self.state.kill_switch,
            trading_enabled=self.inputs.trading_enabled,
            system_ready=True,
            reconciliation_clean=True,
            exchange_healthy=True,
        )

    def _record_equity(self, snapshot: TradingSnapshot, close_time: datetime) -> Decimal:
        equity = valuation(snapshot).equity
        with localcontext(FINANCIAL_CONTEXT):
            exposure = sum(
                (snapshot.held_quantity(s) * snapshot.marks[s] for s in snapshot.marks),
                Decimal(0),
            )
        in_market = any(
            self.inputs.markets[s].tradeable_quantity(snapshot.held_quantity(s), mark) > 0
            for s, mark in snapshot.marks.items()
        )
        self.result.equity.append(EquityPoint(close_time, equity, exposure, in_market))
        return equity

    def _equity_history(self, close_time: datetime) -> tuple[Decimal, Decimal]:
        """(first equity of this UTC day, highest equity so far)."""
        day_start = close_time.replace(hour=0, minute=0, second=0, microsecond=0)
        points = self.result.equity
        first_today = next(p.equity for p in points if p.time >= day_start)
        return first_today, max(p.equity for p in points)

    def _enforce_loss_limits(
        self, snapshot: TradingSnapshot, equity: Decimal, close_time: datetime
    ) -> None:
        kill_switch = self.state.kill_switch
        if kill_switch is None or not kill_switch.allows_new_orders:
            return
        day_start, peak = self._equity_history(close_time)
        with_history = replace(snapshot, day_start_equity=day_start, peak_equity=peak)
        breaches = [
            r
            for r in loss_limit_breaches(with_history, self.inputs.risk, equity)
            if r in TRIP_REASONS
        ]
        if breaches:
            reason = ", ".join(r.value for r in breaches)
            self.state.kill_switch = kill_switch.trip(at=close_time, reason=reason, actor=ACTOR)
            self.result.halted_at = close_time
            self.result.halt_reason = reason

    def _decide(self, symbol: str, open_time: datetime, close_time: datetime) -> None:
        strategy = self.inputs.strategy
        candles = self.by_symbol[symbol]
        lookback = strategy.lookback
        times = [open_time - (lookback - 1 - i) * self.duration for i in range(lookback)]
        window = [candles[t] for t in times if t in candles]
        if window_problem(window, self.inputs.timeframe, lookback) is not None:
            self.result.skipped_windows += 1
            return
        signal = evaluate_candle(
            strategy,
            window=window,
            timeframe=self.inputs.timeframe,
            portfolio=PortfolioView(as_of=close_time),
            created_at=close_time,
        )
        if signal is None:
            return
        self.result.signals += 1
        day_start, peak = self._equity_history(close_time)
        snapshot = replace(
            self._snapshot(close_time),
            day_start_equity=day_start,
            peak_equity=peak,
            market_data_ok=MappingProxyType({symbol: True}),
        )
        sized = size(signal, snapshot, self.inputs.sizing, now=close_time)
        if sized.intent is None:
            key = sized.reason.value
            self.result.not_sized[key] = self.result.not_sized.get(key, 0) + 1
            return
        decision = decide(sized.intent, snapshot, self.inputs.risk, now=close_time)
        self.state.intents_created.append(close_time)
        if not decision.approved:
            for reason in decision.reasons:
                self.result.rejected[reason.value] = self.result.rejected.get(reason.value, 0) + 1
            return
        self.result.approved += 1
        self.state.pending.append(
            Order.new(sized.intent).transition(OrderStatus.RISK_APPROVED, at=close_time)
        )
