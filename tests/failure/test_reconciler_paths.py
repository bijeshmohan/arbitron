"""Reconciler branches beyond the headline matrix rows (docs/failure-matrix.md steps 3-6)."""

from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from arbitron.domain.enums import KillSwitchState, OrderStatus, Side
from arbitron.domain.events import OrderEvent
from arbitron.domain.fills import ExchangeTrade, Fee
from arbitron.domain.orders import Order
from arbitron.paper.broker import PaperBroker
from arbitron.ports import errors as venue
from tests.failure.test_failure_matrix import (
    NOW,
    SYMBOL,
    kill_switch_state,
    only_order,
    session_for,
    stranded_submitting_order,
)
from tests.integration.conftest import UowFactory
from tests.integration.test_market_ingestion import ManualClock

S = OrderStatus


async def needs_reconciliation(uow: UowFactory, order: Order) -> Order:
    updated = order.transition(S.RECONCILIATION_REQUIRED, at=NOW)
    async with uow() as tx:
        await tx.orders.apply_transition(
            order, updated, OrderEvent(order.id, order.status, updated.status, NOW, "c")
        )
        await tx.commit()
    return updated


def trade(exchange_order_id: str, amount: str = "0.1", trade_id: str = "t-x") -> ExchangeTrade:
    return ExchangeTrade(
        "binance",
        trade_id,
        exchange_order_id,
        SYMBOL,
        Side.BUY,
        Decimal(amount),
        Decimal(100),
        Fee(Decimal(0), "BTC"),
        NOW,
    )


class TestRecheck:
    async def test_order_needing_reconciliation_resolves_once_venue_agrees(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        submitting = await stranded_submitting_order(engine, uow, venue_accepts=True)
        await needs_reconciliation(uow, submitting)

        session = await session_for(engine, uow, ManualClock(NOW), script={})

        assert (await only_order(uow)).status is S.FILLED
        assert await kill_switch_state(session) is KillSwitchState.ARMED

    async def test_order_needing_reconciliation_stays_when_venue_disagrees(
        self, engine: AsyncEngine, uow: UowFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        submitting = await stranded_submitting_order(engine, uow, venue_accepts=True)
        await needs_reconciliation(uow, submitting)

        async def no_trades(*_: Any, **__: Any) -> Sequence[ExchangeTrade]:
            return []  # the venue reports a fill but no trades: contradictory

        # Patched before startup, whose reconciliation must also see the contradiction.
        monkeypatch.setattr(PaperBroker, "fetch_trades", no_trades)
        session = await session_for(engine, uow, ManualClock(NOW), script={}, arm=True)
        report = await session.reconciler.run()

        assert (await only_order(uow)).status is S.RECONCILIATION_REQUIRED
        assert any("trades sum" in d for d in report.discrepancies)
        assert await kill_switch_state(session) is KillSwitchState.RECOVERY_REVIEW

    async def test_venue_without_record_is_a_discrepancy(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        submitting = await stranded_submitting_order(engine, uow, venue_accepts=False)
        await needs_reconciliation(uow, submitting)

        session = await session_for(engine, uow, ManualClock(NOW), script={}, arm=False)
        report = await session.reconciler.run()

        assert any("venue has no record" in d for d in report.discrepancies)


class TestFillSync:
    async def test_trade_for_unknown_order_is_a_discrepancy(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        session = await session_for(engine, uow, ManualClock(NOW), script={})

        async def foreign(*_: Any, **__: Any) -> Sequence[ExchangeTrade]:
            return [trade("ex-unknown")]

        session.broker.fetch_trades = foreign  # type: ignore[method-assign]
        report = await session.reconciler.run()

        assert any("unknown order ex-unknown" in d for d in report.discrepancies)

    async def test_extra_trade_beyond_recorded_fill_is_a_discrepancy(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        clock = ManualClock(NOW)
        session = await session_for(engine, uow, clock)
        await session.run_cycle()
        order = await only_order(uow)
        assert order.exchange_order_id is not None
        real = await session.broker.fetch_trades(SYMBOL)
        extra = trade(order.exchange_order_id, "0.001", trade_id="t-extra")

        async def with_extra(*_: Any, **__: Any) -> Sequence[ExchangeTrade]:
            return [*real, extra]

        session.broker.fetch_trades = with_extra  # type: ignore[method-assign]
        report = await session.reconciler.run()

        assert any("would exceed the recorded fill" in d for d in report.discrepancies)


class TestVenueUnavailableDuringChecks:
    @pytest.mark.parametrize("method", ["fetch_trades", "fetch_balances", "fetch_open_orders"])
    async def test_failed_check_is_pending_not_clean(
        self, engine: AsyncEngine, uow: UowFactory, method: str
    ) -> None:
        session = await session_for(engine, uow, ManualClock(NOW), script={})

        async def down(*_: Any, **__: Any) -> Any:
            raise venue.ExchangeUnavailable("maintenance")

        setattr(session.broker, method, down)
        report = await session.reconciler.run()

        assert not report.clean
        assert report.pending
        assert not report.discrepancies
        # Unreachable is not a discrepancy: the kill switch stays armed, but the
        # snapshot's reconciliation flag blocks new orders until a clean run.
        assert await kill_switch_state(session) is KillSwitchState.ARMED

    async def test_venue_that_cannot_look_up_by_client_id_keeps_order_pending(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        clock = ManualClock(NOW)
        await stranded_submitting_order(engine, uow, venue_accepts=False)
        session = await session_for(engine, uow, clock, script={})

        async def unsupported(*_: Any) -> Any:
            raise venue.UnsupportedCapability("no client id lookups")

        session.broker.fetch_order_by_client_id = unsupported  # type: ignore[method-assign]
        clock.advance(timedelta(minutes=1))
        report = await session.reconciler.run()

        assert any("cannot look orders up" in p for p in report.pending)
        assert (await only_order(uow)).status is S.SUBMISSION_UNKNOWN
