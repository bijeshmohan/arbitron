from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from arbitron.domain.enums import KillSwitchState, OrderStatus, Severity, Side
from arbitron.domain.events import OrderEvent, SystemEvent
from arbitron.domain.fills import Fee, Fill
from arbitron.domain.orders import Order
from arbitron.domain.risk import KillSwitch, RiskDecision, RiskReason
from arbitron.ports.repositories import ConcurrencyConflict, ConflictingRecordError
from tests.factories import FIVE_MIN, T0, at, make_candle, make_intent, make_signal, make_trade
from tests.integration.conftest import UowFactory

S = OrderStatus


def created_event(order: Order) -> OrderEvent:
    return OrderEvent(order.id, None, S.CREATED, order.updated_at, "corr-1")


def event_for(previous: Order, updated: Order) -> OrderEvent:
    return OrderEvent(previous.id, previous.status, updated.status, updated.updated_at, "corr-1")


async def persist_new_order(uow: UowFactory) -> Order:
    order = Order.new(make_intent())
    async with uow() as tx:
        assert await tx.signals.add_if_absent(make_signal())
        assert await tx.orders.add_if_absent(order, created_event(order))
        await tx.commit()
    return order


async def transition(uow: UowFactory, previous: Order, target: OrderStatus, **kw: object) -> Order:
    updated = previous.transition(target, **kw)  # type: ignore[arg-type]
    async with uow() as tx:
        await tx.orders.apply_transition(previous, updated, event_for(previous, updated))
        await tx.commit()
    return updated


class TestCandles:
    async def test_upsert_is_idempotent_and_round_trips(self, uow: UowFactory) -> None:
        candle = make_candle(close=Decimal("105.123456789012345678"))
        async with uow() as tx:
            assert await tx.candles.upsert([candle]) == 1
            await tx.commit()
        async with uow() as tx:
            # Identical closed candle: nothing written.
            assert await tx.candles.upsert([candle]) == 0
            stored = await tx.candles.latest_closed("binance", "BTC/USDT", FIVE_MIN)

        assert stored == candle
        assert stored is not None
        assert stored.close == Decimal("105.123456789012345678")
        assert stored.open_time.utcoffset() == timedelta(0)

    async def test_closed_candle_is_never_modified(self, uow: UowFactory) -> None:
        async with uow() as tx:
            await tx.candles.upsert([make_candle()])
            assert await tx.candles.upsert([make_candle(close=Decimal(106))]) == 0
            stored = await tx.candles.latest_closed("binance", "BTC/USDT", FIVE_MIN)

        assert stored is not None
        assert stored.close == Decimal(105)

    async def test_open_candle_updates_then_closes(self, uow: UowFactory) -> None:
        partial = make_candle(is_closed=False, ingested_at=at(2), close=Decimal(101))
        async with uow() as tx:
            await tx.candles.upsert([partial])
            assert await tx.candles.latest_closed("binance", "BTC/USDT", FIVE_MIN) is None
            assert await tx.candles.upsert([make_candle()]) == 1
            stored = await tx.candles.latest_closed("binance", "BTC/USDT", FIVE_MIN)

        assert stored == make_candle()

    async def test_closed_range_bounds_order_and_filters(self, uow: UowFactory) -> None:
        candles = [make_candle(open_time=at(5 * i), ingested_at=at(5 * i + 5)) for i in range(5)]
        other_symbol = make_candle(symbol="ETH/USDT")
        still_open = make_candle(open_time=at(25), is_closed=False, ingested_at=at(26))
        async with uow() as tx:
            await tx.candles.upsert([*reversed(candles), other_symbol, still_open])
            result = await tx.candles.closed_range(
                "binance", "BTC/USDT", FIVE_MIN, start=at(5), end=at(20)
            )

        assert [c.open_time for c in result] == [at(5), at(10), at(15)]

    async def test_large_batches_are_split(self, uow: UowFactory) -> None:
        candles = [
            make_candle(open_time=T0 + timedelta(minutes=5 * i), ingested_at=at(5 * i + 5))
            for i in range(2500)
        ]
        async with uow() as tx:
            assert await tx.candles.upsert(candles) == 2500
            latest = await tx.candles.latest_closed("binance", "BTC/USDT", FIVE_MIN)

        assert latest is not None
        assert latest.open_time == candles[-1].open_time


class TestSignals:
    async def test_duplicate_signal_is_ignored(self, uow: UowFactory) -> None:
        async with uow() as tx:
            assert await tx.signals.add_if_absent(make_signal())
            # Replay at a different wall-clock time is the same signal.
            assert not await tx.signals.add_if_absent(make_signal(created_at=at(9)))
            stored = await tx.signals.get(make_signal().id)

        assert stored == make_signal()

    async def test_conflicting_duplicate_is_refused(self, uow: UowFactory) -> None:
        async with uow() as tx:
            await tx.signals.add_if_absent(make_signal())
            with pytest.raises(ConflictingRecordError, match="different content"):
                await tx.signals.add_if_absent(make_signal(side=Side.SELL))

    async def test_metadata_round_trips(self, uow: UowFactory) -> None:
        signal = make_signal(metadata={"fast": "10", "slow": "30"}, explanation="cross")
        async with uow() as tx:
            await tx.signals.add_if_absent(signal)
            assert await tx.signals.get(signal.id) == signal


class TestOrders:
    async def test_order_requires_persisted_signal(self, uow: UowFactory) -> None:
        order = Order.new(make_intent())
        async with uow() as tx:
            with pytest.raises(IntegrityError, match="fk_orders_signal_id_signals"):
                await tx.orders.add_if_absent(order, created_event(order))

    async def test_new_order_round_trips_with_event(self, uow: UowFactory) -> None:
        order = await persist_new_order(uow)
        async with uow() as tx:
            stored = await tx.orders.get(order.id)
            by_client = await tx.orders.get_by_client_order_id(
                "binance", order.intent.client_order_id
            )
            events = await tx.orders.list_events(order.id)

        assert stored == order
        assert by_client == order
        assert [(e.from_status, e.to_status) for e in events] == [(None, S.CREATED)]

    async def test_duplicate_intent_is_ignored(self, uow: UowFactory) -> None:
        order = await persist_new_order(uow)
        replay = Order.new(make_intent(created_at=at(9)))
        async with uow() as tx:
            assert not await tx.orders.add_if_absent(replay, created_event(replay))
            assert len(await tx.orders.list_events(order.id)) == 1

    async def test_conflicting_intent_is_refused(self, uow: UowFactory) -> None:
        await persist_new_order(uow)
        resized = Order.new(make_intent(amount=Decimal("0.02")))
        async with uow() as tx:
            with pytest.raises(ConflictingRecordError):
                await tx.orders.add_if_absent(resized, created_event(resized))

    async def test_only_created_orders_can_be_added(self, uow: UowFactory) -> None:
        order = Order.new(make_intent()).transition(S.RISK_APPROVED, at=at(6))
        async with uow() as tx:
            with pytest.raises(ValueError, match="must be persisted as created"):
                await tx.orders.add_if_absent(order, created_event(order))

    async def test_lifecycle_is_persisted_and_audited(self, uow: UowFactory) -> None:
        order = await persist_new_order(uow)
        order = await transition(uow, order, S.RISK_APPROVED, at=at(6))
        order = await transition(uow, order, S.SUBMITTING, at=at(7))
        order = await transition(uow, order, S.OPEN, at=at(8), exchange_order_id="ex-1")
        order = await transition(
            uow,
            order,
            S.PARTIALLY_FILLED,
            at=at(9),
            filled_amount=Decimal("0.004"),
            average_fill_price=Decimal("49990.123456789012345678"),
        )
        async with uow() as tx:
            stored = await tx.orders.get(order.id)
            events = await tx.orders.list_events(order.id)
            open_orders = await tx.orders.list_non_terminal("binance")

        assert stored == order
        assert [e.to_status for e in events] == [
            S.CREATED,
            S.RISK_APPROVED,
            S.SUBMITTING,
            S.OPEN,
            S.PARTIALLY_FILLED,
        ]
        assert open_orders == [order]

        await transition(uow, order, S.FILLED, at=at(10), filled_amount=Decimal("0.010"))
        async with uow() as tx:
            assert await tx.orders.list_non_terminal("binance") == []

    async def test_stale_transition_raises_concurrency_conflict(self, uow: UowFactory) -> None:
        order = await persist_new_order(uow)
        await transition(uow, order, S.RISK_APPROVED, at=at(6))

        stale = order.transition(S.RISK_REJECTED, at=at(7))
        async with uow() as tx:
            with pytest.raises(ConcurrencyConflict):
                await tx.orders.apply_transition(order, stale, event_for(order, stale))

    async def test_disallowed_transition_is_refused(self, uow: UowFactory) -> None:
        order = await persist_new_order(uow)
        forged = replace(order, status=S.FILLED, filled_amount=Decimal("0.010"), updated_at=at(7))
        async with uow() as tx:
            with pytest.raises(ValueError, match="not an allowed transition"):
                await tx.orders.apply_transition(order, forged, event_for(order, forged))

    async def test_mismatched_event_is_refused(self, uow: UowFactory) -> None:
        order = await persist_new_order(uow)
        approved = order.transition(S.RISK_APPROVED, at=at(6))
        wrong = OrderEvent(order.id, S.CREATED, S.RISK_REJECTED, at(6), "corr-1")
        async with uow() as tx:
            with pytest.raises(ValueError, match="does not describe"):
                await tx.orders.apply_transition(order, approved, wrong)

    async def test_uncommitted_work_is_rolled_back(self, uow: UowFactory) -> None:
        order = Order.new(make_intent())
        async with uow() as tx:
            await tx.signals.add_if_absent(make_signal())
            await tx.orders.add_if_absent(order, created_event(order))
        async with uow() as tx:
            assert await tx.orders.get(order.id) is None
            assert await tx.signals.get(make_signal().id) is None

    async def test_unit_of_work_is_single_use(self, uow: UowFactory) -> None:
        tx = uow()
        async with tx:
            pass
        with pytest.raises(RuntimeError, match="single-use"):
            async with tx:
                pass


class TestFillsAndRisk:
    async def test_fill_dedupe_and_conflict(self, uow: UowFactory) -> None:
        order = await persist_new_order(uow)
        fill = Fill(order.id, make_trade(fee=Fee(Decimal("0.000004"), "BTC")))
        async with uow() as tx:
            assert await tx.fills.add_if_absent(fill)
            assert not await tx.fills.add_if_absent(fill)
            with pytest.raises(ConflictingRecordError):
                await tx.fills.add_if_absent(Fill(order.id, make_trade(amount=Decimal("0.005"))))
            assert await tx.fills.list_for_order(order.id) == [fill]

    async def test_fill_without_trade_id_or_fee(self, uow: UowFactory) -> None:
        order = await persist_new_order(uow)
        fill = Fill(order.id, make_trade(trade_id=None, fee=None))
        async with uow() as tx:
            assert await tx.fills.add_if_absent(fill)
            assert await tx.fills.list_for_order(order.id) == [fill]

    async def test_risk_decision_is_write_once(self, uow: UowFactory) -> None:
        order = await persist_new_order(uow)
        decision = RiskDecision(
            order.id,
            (RiskReason.MAX_ORDER_NOTIONAL, RiskReason.MARKET_DATA_STALE),
            "risk-v1",
            at(6),
            {"order_notional": "500.00", "limit": "50"},
        )
        async with uow() as tx:
            await tx.risk_decisions.add(decision)
            assert await tx.risk_decisions.get_for_intent(order.id) == decision
            with pytest.raises(ConflictingRecordError):
                await tx.risk_decisions.add(replace(decision, reasons=()))


class TestKillSwitch:
    async def test_lifecycle_and_history(self, uow: UowFactory, engine: AsyncEngine) -> None:
        armed = KillSwitch(KillSwitchState.ARMED, T0, "system", "initialized")
        async with uow() as tx:
            assert await tx.kill_switch.get() is None
            await tx.kill_switch.save(None, armed)
            await tx.commit()

        tripped = armed.trip(at=at(1), reason="daily loss limit")
        async with uow() as tx:
            current = await tx.kill_switch.get()
            assert current == armed
            await tx.kill_switch.save(current, tripped)
            await tx.commit()

        async with uow() as tx:
            assert await tx.kill_switch.get() == tripped
            # Saving from a stale read must fail.
            with pytest.raises(ConcurrencyConflict):
                await tx.kill_switch.save(armed, armed.trip(at=at(2), reason="dup"))

        async with engine.connect() as conn:
            history = (
                await conn.execute(
                    text("SELECT from_state, to_state FROM kill_switch_events ORDER BY id")
                )
            ).all()
        assert [tuple(row) for row in history] == [(None, "armed"), ("armed", "tripped")]

    async def test_double_initialization_conflicts(self, uow: UowFactory) -> None:
        armed = KillSwitch(KillSwitchState.ARMED, T0, "system", "initialized")
        async with uow() as tx:
            await tx.kill_switch.save(None, armed)
            with pytest.raises(ConcurrencyConflict):
                await tx.kill_switch.save(None, armed)

    async def test_illegal_transition_is_refused(self, uow: UowFactory) -> None:
        tripped = KillSwitch(KillSwitchState.TRIPPED, T0, "system", "t")
        forged = KillSwitch(KillSwitchState.ARMED, at(1), "system", "skip review")
        async with uow() as tx:
            await tx.kill_switch.save(None, tripped)
            with pytest.raises(ValueError, match="tripped -> armed"):
                await tx.kill_switch.save(tripped, forged)


class TestSystemEvents:
    async def test_append_and_list_recent(self, uow: UowFactory) -> None:
        events = [
            SystemEvent(f"e{i}", Severity.INFO, "test", at(i), f"c{i}", {"i": i}) for i in range(3)
        ]
        async with uow() as tx:
            for event in events:
                await tx.system_events.append(event)
            recent = await tx.system_events.list_recent(2)

        assert recent == [events[2], events[1]]


class TestDatabaseGuards:
    @pytest.mark.parametrize(
        "statement",
        [
            "UPDATE order_events SET correlation_id = 'x'",
            "DELETE FROM order_events",
        ],
    )
    async def test_audit_tables_are_append_only(
        self, uow: UowFactory, engine: AsyncEngine, statement: str
    ) -> None:
        await persist_new_order(uow)
        async with engine.begin() as conn:
            with pytest.raises(DBAPIError, match="append-only"):
                await conn.execute(text(statement))

    @pytest.mark.parametrize(
        "table", ["fills", "risk_decisions", "kill_switch_events", "system_events"]
    )
    async def test_every_audit_table_has_the_trigger(self, engine: AsyncEngine, table: str) -> None:
        async with engine.connect() as conn:
            exists = await conn.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_trigger "
                    "WHERE tgname = :name AND NOT tgisinternal)"
                ),
                {"name": f"{table}_append_only"},
            )
        assert exists

    async def test_check_constraints_reject_invalid_rows(
        self, uow: UowFactory, engine: AsyncEngine
    ) -> None:
        order = await persist_new_order(uow)
        async with engine.begin() as conn:
            with pytest.raises(IntegrityError, match="ck_orders_status"):
                await conn.execute(
                    text("UPDATE orders SET status = 'bogus' WHERE id = :id"), {"id": order.id}
                )
        async with engine.begin() as conn:
            with pytest.raises(IntegrityError, match="ck_orders_filled_range"):
                await conn.execute(
                    text("UPDATE orders SET filled_amount = amount + 1 WHERE id = :id"),
                    {"id": order.id},
                )

    async def test_tampered_order_row_fails_on_read(
        self, uow: UowFactory, engine: AsyncEngine
    ) -> None:
        order = await persist_new_order(uow)
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE orders SET client_order_id = 'arbforged' WHERE id = :id"),
                {"id": order.id},
            )
        async with uow() as tx:
            with pytest.raises(ValueError, match="client order id"):
                await tx.orders.get(order.id)
