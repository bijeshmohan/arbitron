"""Paper venue, order manager, and the full paper session against real PostgreSQL."""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from arbitron.application.paper_session import PaperSession
from arbitron.application.policies import risk_policy, sizing_policy
from arbitron.clocks import ManualClock
from arbitron.config.settings import ArbitronConfig
from arbitron.domain.enums import KillSwitchState, OrderStatus, Side
from arbitron.domain.fills import ExchangeTrade, Fee
from arbitron.domain.orders import ExchangeOrder, OrderIntent, intent_id_for
from arbitron.domain.strategies import ParameterValue
from arbitron.execution.order_manager import Action, OrderManager
from arbitron.paper.broker import PaperBroker
from arbitron.persistence.db import create_session_factory
from arbitron.ports import errors as venue
from arbitron.simulation.fill_model import FillModel
from arbitron.strategy.base import SignalProposal, StrategyContext
from tests.factories import FIVE_MIN, T0, make_intent, make_signal
from tests.integration.conftest import UowFactory
from tests.integration.test_market_ingestion import MARKET, NOW, FakeVenue, history
from tests.unit.test_config import example_data

S = OrderStatus
SYMBOL = "BTC/USDT"
MODEL = FillModel(slippage_bps=Decimal(5), fee_rate=Decimal("0.001"))


def candle_time(index: int) -> datetime:
    return T0 + index * FIVE_MIN.duration


class ScriptedStrategy:
    """Emits the scripted side on the scripted candles; nothing otherwise."""

    strategy_id = "scripted"
    version = "1"
    lookback = 2

    def __init__(self, script: Mapping[int, Side]) -> None:
        self.script = {candle_time(i): side for i, side in script.items()}
        self.parameters: Mapping[str, ParameterValue] = {
            "script": ",".join(f"{t.isoformat()}={s.value}" for t, s in sorted(self.script.items()))
        }

    def evaluate(self, context: StrategyContext) -> Sequence[SignalProposal]:
        side = self.script.get(context.current.open_time)
        return [SignalProposal(side, f"scripted_{side.value}")] if side else []


def config(**sections: dict[str, Any]) -> ArbitronConfig:
    data = example_data()
    data["market"] = {"backfill_candles": 50}
    for name, values in sections.items():
        data[name] = {**data.get(name, {}), **values}
    return ArbitronConfig.model_validate(data)


async def make_session(
    engine: AsyncEngine,
    uow: UowFactory,
    venue_: FakeVenue,
    strategy: ScriptedStrategy,
    cfg: ArbitronConfig | None = None,
    *,
    arm: bool = True,
) -> PaperSession:
    session = PaperSession(
        cfg or config(),
        strategy=strategy,  # type: ignore[arg-type]
        market_data=venue_,
        session_factory=create_session_factory(engine),
        uow_factory=uow,
        clock=venue_.clock,
    )
    await session.start()
    if arm and (await session.kill_switch.status()).state is not KillSwitchState.ARMED:  # type: ignore[union-attr]
        await session.kill_switch.arm(operator="tester", acknowledgement="test run")
    return session


async def count(engine: AsyncEngine, sql: str) -> int:
    async with engine.connect() as conn:
        return int(await conn.scalar(text(sql)) or 0)


class TestPaperSessionEndToEnd:
    async def test_buy_then_sell_round_trip_is_fully_audited(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        """Acceptance: signals, decisions, orders, fills, positions, snapshots; no duplicates."""
        clock = ManualClock(NOW)  # candles 0..99 closed; 100 in progress
        venue_ = FakeVenue(clock, history(200))
        strategy = ScriptedStrategy({99: Side.BUY, 100: Side.SELL})
        session = await make_session(engine, uow, venue_, strategy)

        first = await session.run_cycle()
        assert [o.action for o in first.orders] == [Action.EXECUTED]
        buy = first.orders[0].order
        assert buy is not None
        assert buy.status is S.FILLED
        assert buy.intent.side is Side.BUY

        clock.advance(FIVE_MIN.duration)  # candle 100 closes
        second = await session.run_cycle()
        assert [o.action for o in second.orders] == [Action.EXECUTED]
        sell = second.orders[0].order
        assert sell is not None
        assert (sell.status, sell.intent.side) == (S.FILLED, Side.SELL)

        # Restart: a new session on the same database acts on nothing twice.
        restarted = await make_session(
            engine, uow, venue_, ScriptedStrategy({99: Side.BUY, 100: Side.SELL})
        )
        third = await restarted.run_cycle()
        assert [o.action for o in third.orders] == [Action.ALREADY_HANDLED]

        assert await count(engine, "SELECT count(*) FROM signals") == 2
        assert await count(engine, "SELECT count(*) FROM orders") == 2
        assert await count(engine, "SELECT count(*) FROM risk_decisions WHERE approved") == 2
        assert await count(engine, "SELECT count(*) FROM fills") == 2
        assert await count(engine, "SELECT count(*) FROM paper_trades") == 2
        assert await count(engine, "SELECT count(*) FROM paper_orders") == 2
        assert await count(engine, "SELECT count(*) FROM equity_snapshots") >= 3
        # created -> risk_approved -> submitting -> filled, for each order
        assert await count(engine, "SELECT count(*) FROM order_events") == 8

        async with uow() as tx:
            position = await tx.positions.get("binance", SYMBOL)
        balances = await restarted.broker.fetch_balances()
        assert position is not None
        # The buy paid its fee in BTC; selling "everything" leaves sub-lot dust.
        assert 0 < position.quantity < MARKET.amount_step * 100
        assert balances["BTC"].total == position.quantity  # projection matches the venue
        assert position.fees_paid > 0
        # Accounting identity: cash change = realized P&L - cost basis still held (dust).
        cash_change = balances["USDT"].total - Decimal(1000)
        identity = position.realized_pnl - position.quantity * position.average_cost
        assert abs(cash_change - identity) < Decimal("1E-15")

    async def test_unarmed_kill_switch_rejects_and_nothing_reaches_the_venue(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        venue_ = FakeVenue(ManualClock(NOW), history(200))
        session = await make_session(
            engine, uow, venue_, ScriptedStrategy({99: Side.BUY}), arm=False
        )

        outcome = await session.run_cycle()

        assert [o.action for o in outcome.orders] == [Action.RISK_REJECTED]
        assert "kill_switch_not_armed" in outcome.orders[0].detail
        assert await count(engine, "SELECT count(*) FROM paper_orders") == 0

    async def test_stale_signal_is_not_traded(self, engine: AsyncEngine, uow: UowFactory) -> None:
        clock = ManualClock(NOW)
        venue_ = FakeVenue(clock, history(200))
        session = await make_session(engine, uow, venue_, ScriptedStrategy({99: Side.BUY}))
        # First cycle happens late: more than one interval after candle 99 closed,
        # but before candle 101 closes, so candle 100 is the latest.
        clock.set(candle_time(101) + timedelta(minutes=1))

        outcome = await session.run_cycle()

        assert outcome.orders == ()
        assert await count(engine, "SELECT count(*) FROM orders") == 0

    async def test_daily_loss_breach_trips_the_kill_switch(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        clock = ManualClock(NOW)
        venue_ = FakeVenue(clock, history(200))
        # Buy at candle 99, then the market collapses at candle 100.
        venue_.history[100] = replace(
            venue_.history[100],
            open=Decimal(1),
            high=Decimal(1),
            low=Decimal("0.5"),
            close=Decimal("0.5"),
        )
        cfg = config(risk={"max_daily_loss": "10"})
        session = await make_session(engine, uow, venue_, ScriptedStrategy({99: Side.BUY}), cfg)
        await session.run_cycle()

        clock.advance(FIVE_MIN.duration)
        outcome = await session.run_cycle()

        assert outcome.tripped
        state = await session.kill_switch.status()
        assert state is not None
        assert state.state is KillSwitchState.TRIPPED
        assert "automatic" in state.reason


class StubExecution:
    """ExecutionPort double for failure paths the paper venue cannot produce."""

    def __init__(self, *, submit: Any = None, trades: Sequence[ExchangeTrade] = ()) -> None:
        self.submit = submit
        self.trades = list(trades)
        self.submitted: list[OrderIntent] = []

    async def submit_order(self, intent: OrderIntent) -> ExchangeOrder:
        self.submitted.append(intent)
        result = self.submit(intent) if callable(self.submit) else self.submit
        if isinstance(result, BaseException):
            raise result
        if asyncio.iscoroutine(result):
            return await result
        return result  # type: ignore[no-any-return]

    async def fetch_trades(self, symbol: str, **_: Any) -> Sequence[ExchangeTrade]:
        return self.trades

    async def cancel_order(self, *_: Any) -> None: ...
    async def fetch_order(self, *_: Any) -> None: ...
    async def fetch_order_by_client_id(self, *_: Any) -> None: ...
    async def fetch_open_orders(self, *_: Any) -> list[ExchangeOrder]:
        return []

    async def close(self) -> None: ...


def venue_order(
    intent: OrderIntent, *, filled: str, status: S = S.FILLED, client: str | None = None
) -> ExchangeOrder:
    return ExchangeOrder(
        exchange_id="binance",
        exchange_order_id="ex-1",
        client_order_id=client or intent.client_order_id,
        symbol=intent.symbol,
        side=intent.side,
        order_type=intent.order_type,
        status=status,
        raw_status=status.value.upper(),
        amount=intent.amount,
        filled=Decimal(filled),
        remaining=intent.amount - Decimal(filled),
        average_price=Decimal("100.07") if Decimal(filled) else None,
        fee=None,
        timestamp=NOW,
    )


def venue_trade(amount: str, fee_currency: str = "BTC") -> ExchangeTrade:
    return ExchangeTrade(
        "binance",
        "t-1",
        "ex-1",
        SYMBOL,
        Side.BUY,
        Decimal(amount),
        Decimal("100.07"),
        Fee(Decimal("0.0001"), fee_currency),
        NOW,
    )


class TestOrderManagerFailurePaths:
    async def manager_and_signal(
        self, engine: AsyncEngine, uow: UowFactory, execution: Any, submit_seconds: float = 5
    ) -> tuple[OrderManager, Any, Any]:
        cfg = config()
        venue_ = FakeVenue(ManualClock(NOW), history(200))
        session = await make_session(engine, uow, venue_, ScriptedStrategy({99: Side.BUY}))
        outcome = await session.market.run_cycle()
        snapshot = await session._snapshots.build(  # pyright: ignore[reportPrivateUsage]
            {r.symbol: r for r in outcome.reports}, system_ready=True
        )
        manager = OrderManager(
            execution=execution,
            uow_factory=uow,
            clock=venue_.clock,
            sizing_policy=sizing_policy(cfg),
            risk_policy=risk_policy(cfg),
            submission_timeout=timedelta(seconds=submit_seconds),
        )
        signal = make_signal(source_candle_time=candle_time(99), created_at=NOW)
        async with uow() as tx:
            from tests.factories import SMA_CONFIG  # noqa: PLC0415

            await tx.strategy_configs.add_if_absent(SMA_CONFIG, created_at=T0)
            await tx.signals.add_if_absent(signal)
            await tx.commit()
        return manager, signal, snapshot

    async def test_timeout_leaves_submission_unknown_and_blocks_new_orders(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        async def hang(_: OrderIntent) -> ExchangeOrder:
            await asyncio.sleep(10)
            raise AssertionError("unreachable")

        manager, signal, snapshot = await self.manager_and_signal(
            engine, uow, StubExecution(submit=hang), submit_seconds=0.05
        )

        outcome = await manager.act_on_signal(signal, snapshot)

        assert outcome.action is Action.SUBMISSION_UNKNOWN
        assert outcome.order is not None
        assert outcome.order.status is S.SUBMISSION_UNKNOWN
        assert (
            await count(
                engine,
                "SELECT count(*) FROM system_events WHERE event_type = 'order.submission_unknown'",
            )
            == 1
        )
        # Acting again never re-submits: the intent already exists.
        again = await manager.act_on_signal(signal, snapshot)
        assert again.action is Action.ALREADY_HANDLED

    async def test_cancellation_mid_submission_is_recorded_as_unknown(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        started = asyncio.Event()

        async def hang(_: OrderIntent) -> ExchangeOrder:
            started.set()
            await asyncio.sleep(10)
            raise AssertionError("unreachable")

        manager, signal, snapshot = await self.manager_and_signal(
            engine, uow, StubExecution(submit=hang)
        )
        task = asyncio.create_task(manager.act_on_signal(signal, snapshot))
        await started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        async with uow() as tx:
            order = await tx.orders.get(intent_id_for(signal.id))
        assert order is not None
        assert order.status is S.SUBMISSION_UNKNOWN

    @pytest.mark.parametrize(
        "error",
        [
            venue.NetworkTransient("reset"),
            venue.RateLimited("429"),
            venue.UnknownExchangeError("?"),
        ],
    )
    async def test_ambiguous_venue_errors_are_unknown(
        self, engine: AsyncEngine, uow: UowFactory, error: Exception
    ) -> None:
        manager, signal, snapshot = await self.manager_and_signal(
            engine, uow, StubExecution(submit=error)
        )

        outcome = await manager.act_on_signal(signal, snapshot)

        assert outcome.action is Action.SUBMISSION_UNKNOWN

    @pytest.mark.parametrize(
        "error",
        [
            venue.InvalidOrder("LOT_SIZE"),
            venue.InsufficientFunds("poor"),
            venue.MarketUnavailable("halt"),
        ],
    )
    async def test_definitive_rejections_are_terminal(
        self, engine: AsyncEngine, uow: UowFactory, error: Exception
    ) -> None:
        manager, signal, snapshot = await self.manager_and_signal(
            engine, uow, StubExecution(submit=error)
        )

        outcome = await manager.act_on_signal(signal, snapshot)

        assert outcome.action is Action.EXCHANGE_REJECTED
        assert outcome.order is not None
        assert outcome.order.status is S.EXCHANGE_REJECTED

    async def test_fill_mismatch_requires_reconciliation(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        def filled(intent: OrderIntent) -> ExchangeOrder:
            return venue_order(intent, filled=str(intent.amount))

        execution = StubExecution(submit=filled, trades=[])
        manager, signal, snapshot = await self.manager_and_signal(engine, uow, execution)

        outcome = await manager.act_on_signal(signal, snapshot)

        assert outcome.order is not None
        assert outcome.order.status is S.RECONCILIATION_REQUIRED
        assert (
            await count(
                engine,
                "SELECT count(*) FROM system_events "
                "WHERE event_type = 'order.venue_result_discrepancy'",
            )
            == 1
        )

    async def test_unsupported_fee_currency_requires_reconciliation(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        def filled(intent: OrderIntent) -> ExchangeOrder:
            execution.trades = [venue_trade(str(intent.amount), fee_currency="BNB")]
            return venue_order(intent, filled=str(intent.amount))

        execution = StubExecution(submit=filled)
        manager, signal, snapshot = await self.manager_and_signal(engine, uow, execution)

        outcome = await manager.act_on_signal(signal, snapshot)

        assert outcome.order is not None
        assert outcome.order.status is S.RECONCILIATION_REQUIRED
        async with uow() as tx:
            assert await tx.positions.get("binance", SYMBOL) is None  # never half-applied

    async def test_wrong_client_id_requires_reconciliation(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        def foreign(intent: OrderIntent) -> ExchangeOrder:
            return venue_order(intent, filled="0", status=S.EXPIRED, client="arbsomeoneelse")

        execution = StubExecution(submit=foreign)
        manager, signal, snapshot = await self.manager_and_signal(engine, uow, execution)

        outcome = await manager.act_on_signal(signal, snapshot)

        assert outcome.order is not None
        assert outcome.order.status is S.RECONCILIATION_REQUIRED


class TestPaperBroker:
    def broker(
        self, engine: AsyncEngine, venue_: FakeVenue, model: FillModel = MODEL
    ) -> PaperBroker:
        return PaperBroker(
            session_factory=create_session_factory(engine),
            account="t",
            exchange_id="binance",
            quote_currency="USDT",
            market_data=venue_,
            markets=lambda: {SYMBOL: MARKET},
            fill_model=model,
            clock=venue_.clock,
        )

    def intent(self, **overrides: Any) -> OrderIntent:
        from arbitron.domain.enums import TimeInForce  # noqa: PLC0415

        fields: dict[str, Any] = {
            "amount": Decimal("0.249"),
            "limit_price": Decimal("200.00"),
            "time_in_force": TimeInForce.IOC,
            "created_at": NOW,
        }
        fields.update(overrides)
        return make_intent(**fields)

    async def test_funding_is_once_and_audited(self, engine: AsyncEngine) -> None:
        broker = self.broker(engine, FakeVenue(ManualClock(NOW), history(200)))

        assert await broker.ensure_funded(Decimal(1000))
        assert not await broker.ensure_funded(Decimal(5000))
        assert (await broker.fetch_balances())["USDT"].free == Decimal(1000)
        assert await count(engine, "SELECT count(*) FROM paper_funding") == 1

    async def test_fill_moves_balances_and_records_trade(self, engine: AsyncEngine) -> None:
        venue_ = FakeVenue(ManualClock(NOW), history(200))
        broker = self.broker(engine, venue_)
        await broker.ensure_funded(Decimal(1000))

        order = await broker.submit_order(self.intent())

        assert order.status is S.FILLED
        trades = await broker.fetch_trades(SYMBOL)
        balances = await broker.fetch_balances()
        assert len(trades) == 1
        assert trades[0].fee == Fee(Decimal("0.000249"), "BTC")
        assert balances["BTC"].free == Decimal("0.248751")
        assert balances["USDT"].free == Decimal(1000) - Decimal("0.249") * trades[0].price
        assert await broker.fetch_order_by_client_id(order.client_order_id or "", SYMBOL) == order

    async def test_ioc_beyond_limit_expires_without_trade(self, engine: AsyncEngine) -> None:
        broker = self.broker(engine, FakeVenue(ManualClock(NOW), history(200)))
        await broker.ensure_funded(Decimal(1000))

        order = await broker.submit_order(self.intent(limit_price=Decimal("50.00")))

        assert (order.status, order.filled) == (S.EXPIRED, 0)
        assert await broker.fetch_trades(SYMBOL) == []
        assert (await broker.fetch_balances())["USDT"].free == Decimal(1000)

    async def test_partial_fill_is_partially_filled_closed(self, engine: AsyncEngine) -> None:
        broker = self.broker(
            engine,
            FakeVenue(ManualClock(NOW), history(200)),
            replace(MODEL, fill_ratio=Decimal("0.5")),
        )
        await broker.ensure_funded(Decimal(1000))

        order = await broker.submit_order(self.intent())

        assert order.status is S.PARTIALLY_FILLED_CLOSED
        assert order.filled == Decimal("0.12450")  # 0.1245 on the 0.00001 lot

    @pytest.mark.parametrize(
        ("overrides", "error"),
        [
            ({"amount": Decimal("0.0000001")}, "LOT_SIZE"),
            ({"limit_price": Decimal("200.001")}, "PRICE_FILTER"),
            ({"amount": Decimal("0.00001")}, "NOTIONAL"),
        ],
    )
    async def test_venue_filters(
        self, engine: AsyncEngine, overrides: dict[str, Any], error: str
    ) -> None:
        broker = self.broker(engine, FakeVenue(ManualClock(NOW), history(200)))
        await broker.ensure_funded(Decimal(1000))

        with pytest.raises(venue.InvalidOrder, match=error):
            await broker.submit_order(self.intent(**overrides))

    async def test_insufficient_funds_reserved_at_limit(self, engine: AsyncEngine) -> None:
        broker = self.broker(engine, FakeVenue(ManualClock(NOW), history(200)))
        await broker.ensure_funded(Decimal(40))

        # Needs 0.249 * 200 = 49.8 at the limit even though it would fill near 100.
        with pytest.raises(venue.InsufficientFunds):
            await broker.submit_order(self.intent())

    async def test_cannot_sell_what_is_not_held(self, engine: AsyncEngine) -> None:
        broker = self.broker(engine, FakeVenue(ManualClock(NOW), history(200)))
        await broker.ensure_funded(Decimal(1000))

        with pytest.raises(venue.InsufficientFunds):
            await broker.submit_order(self.intent(side=Side.SELL, limit_price=Decimal("50.00")))

    async def test_duplicate_client_order_id_is_ambiguous(self, engine: AsyncEngine) -> None:
        broker = self.broker(engine, FakeVenue(ManualClock(NOW), history(200)))
        await broker.ensure_funded(Decimal(1000))
        await broker.submit_order(self.intent())

        with pytest.raises(venue.UnknownExchangeError, match="duplicate"):
            await broker.submit_order(self.intent())

    async def test_ticker_failure_means_not_placed(self, engine: AsyncEngine) -> None:
        venue_ = FakeVenue(ManualClock(NOW), history(200))
        broker = self.broker(engine, venue_)
        await broker.ensure_funded(Decimal(1000))
        venue_.ticker_failures = [venue.ExchangeUnavailable("down")]

        with pytest.raises(venue.ExchangeUnavailable):
            await broker.submit_order(self.intent())
        assert await broker.fetch_order_by_client_id(self.intent().client_order_id, SYMBOL) is None

    async def test_paper_trades_are_append_only(self, engine: AsyncEngine) -> None:
        from sqlalchemy.exc import DBAPIError  # noqa: PLC0415

        broker = self.broker(engine, FakeVenue(ManualClock(NOW), history(200)))
        await broker.ensure_funded(Decimal(1000))
        await broker.submit_order(self.intent())
        async with engine.begin() as conn:
            with pytest.raises(DBAPIError, match="append-only"):
                await conn.execute(text("UPDATE paper_trades SET amount = 1"))
