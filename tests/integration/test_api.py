"""Operational API: authentication, read views, audited operator actions (Phase 10)."""

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from arbitron.api.auth import ApiTokenError
from arbitron.application.paper_runtime import PaperRuntime
from arbitron.clocks import ManualClock
from arbitron.domain.enums import KillSwitchState, Side
from tests.integration.conftest import UowFactory
from tests.integration.test_market_ingestion import NOW, FakeVenue, history
from tests.integration.test_paper_trading import ScriptedStrategy, config, make_session
from tests.integration.test_supervision import RecordingNotifier

API_TOKEN = "test-token-0123456789abcdef0123456789abcdef"  # fake, test-only
AUTH = {"Authorization": f"Bearer {API_TOKEN}"}

READS = [
    "/ready",
    "/metrics",
    "/status",
    "/portfolio",
    "/positions",
    "/orders",
    "/signals",
    "/risk/decisions",
    "/system/events",
]
ACTIONS = [
    ("/kill-switch/trip", {"operator": "mallory", "reason": "x"}),
    ("/reconciliation", {"operator": "mallory"}),
]


async def runtime(engine: AsyncEngine, uow: UowFactory, *, trade: bool = True) -> PaperRuntime:
    venue = FakeVenue(ManualClock(NOW), history(200))
    session = await make_session(engine, uow, venue, ScriptedStrategy({99: Side.BUY}))
    if trade:
        await session.run_cycle()  # one signal, decision, order, fill, position
    return PaperRuntime(
        config(),
        session=session,
        notifier=RecordingNotifier(),
        uow_factory=uow,
        clock=venue.clock,
        lock=None,
        api_token=API_TOKEN,
    )


def client(rt: PaperRuntime) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=rt.api_app()), base_url="http://t")


async def kill_switch_state(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        return str(await conn.scalar(text("SELECT state FROM kill_switch")))


class TestAuthentication:
    async def test_health_is_public_and_minimal(self, engine: AsyncEngine, uow: UowFactory) -> None:
        async with client(await runtime(engine, uow, trade=False)) as c:
            response = await c.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "alive"}

    @pytest.mark.parametrize(
        "headers",
        [
            {},
            {"Authorization": "Bearer wrong-token-0123456789abcdef0123456789"},
            {"Authorization": f"Basic {API_TOKEN}"},
            {"Authorization": API_TOKEN},
            {"Authorization": f"Bearer {API_TOKEN[:-1]}"},
        ],
    )
    async def test_every_other_endpoint_needs_the_token(
        self, engine: AsyncEngine, uow: UowFactory, headers: dict[str, str]
    ) -> None:
        rt = await runtime(engine, uow, trade=False)
        async with client(rt) as c:
            for path in READS:
                response = await c.get(path, headers=headers)
                assert response.status_code == 401, path
                assert response.headers["www-authenticate"] == "Bearer"
            for path, body in ACTIONS:
                assert (await c.post(path, json=body, headers=headers)).status_code == 401, path

        assert rt.metrics.api_auth_failures._value.get() == len(READS) + len(ACTIONS)  # pyright: ignore[reportPrivateUsage]

    async def test_unauthenticated_requests_cannot_change_trading_state(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        async with client(await runtime(engine, uow, trade=False)) as c:
            await c.post("/kill-switch/trip", json={"operator": "mallory", "reason": "x"})
            await c.post("/reconciliation", json={"operator": "mallory"})

        assert await kill_switch_state(engine) == "armed"
        async with engine.connect() as conn:
            operator_events = await conn.scalar(
                text("SELECT count(*) FROM system_events WHERE event_type LIKE 'operator.%'")
            )
        assert operator_events == 0

    async def test_no_docs_and_no_other_writes(self, engine: AsyncEngine, uow: UowFactory) -> None:
        async with client(await runtime(engine, uow, trade=False)) as c:
            assert (await c.get("/docs", headers=AUTH)).status_code == 404
            assert (await c.get("/openapi.json", headers=AUTH)).status_code == 404
            assert (await c.post("/kill-switch/arm", headers=AUTH)).status_code == 404
            assert (await c.post("/status", headers=AUTH)).status_code == 405

    async def test_runtime_refuses_a_missing_or_weak_token(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        venue = FakeVenue(ManualClock(NOW), history(200))
        session = await make_session(engine, uow, venue, ScriptedStrategy({}))
        for token in (None, "short", f" {API_TOKEN}"):
            with pytest.raises(ApiTokenError):
                PaperRuntime(
                    config(),
                    session=session,
                    notifier=RecordingNotifier(),
                    uow_factory=uow,
                    clock=venue.clock,
                    lock=None,
                    api_token=token,
                )


class TestReads:
    async def test_status(self, engine: AsyncEngine, uow: UowFactory) -> None:
        async with client(await runtime(engine, uow)) as c:
            body = (await c.get("/status", headers=AUTH)).json()

        assert body["mode"] == "paper"
        assert body["simulated"] is True
        assert body["kill_switch"]["state"] == "armed"
        assert body["kill_switch"]["actor"] == "tester"
        assert body["equity"]["latest"] is not None
        assert body["ready"] is True  # started, armed, reconciliation clean
        assert body["checks"]["kill_switch"] == "ok"

    async def test_portfolio_orders_signals_decisions_events(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        async with client(await runtime(engine, uow)) as c:
            portfolio = (await c.get("/portfolio", headers=AUTH)).json()
            positions = (await c.get("/positions", headers=AUTH)).json()["positions"]
            orders = (await c.get("/orders?limit=5", headers=AUTH)).json()["orders"]
            signals = (await c.get("/signals", headers=AUTH)).json()["signals"]
            decisions = (await c.get("/risk/decisions", headers=AUTH)).json()["decisions"]
            events = (await c.get("/system/events", headers=AUTH)).json()["events"]

        assert portfolio["simulated"] is True
        assert set(portfolio["balances"]) == {"BTC", "USDT"}
        assert portfolio["balances"]["USDT"]["total"] != "1000"  # the buy spent some
        assert [p["symbol"] for p in positions] == ["BTC/USDT"]
        assert [(o["side"], o["status"]) for o in orders] == [("buy", "filled")]
        assert orders[0]["limit_price"] is not None
        assert [s["side"] for s in signals] == ["buy"]
        assert decisions[0]["approved"] is True
        assert "observations" in decisions[0]
        assert any(e["type"] == "paper.funded" for e in events)

    async def test_limit_is_bounded(self, engine: AsyncEngine, uow: UowFactory) -> None:
        async with client(await runtime(engine, uow, trade=False)) as c:
            assert (await c.get("/orders?limit=0", headers=AUTH)).status_code == 422
            assert (await c.get("/orders?limit=501", headers=AUTH)).status_code == 422

    async def test_metrics_and_ready(self, engine: AsyncEngine, uow: UowFactory) -> None:
        async with client(await runtime(engine, uow, trade=False)) as c:
            metrics = await c.get("/metrics", headers=AUTH)
            ready = await c.get("/ready", headers=AUTH)

        assert "arbitron_operator_actions_total" in metrics.text
        assert ready.status_code == 200
        assert ready.json()["checks"]["reconciliation"] == "ok"

    async def test_no_secrets_in_responses(
        self, engine: AsyncEngine, uow: UowFactory, database_url: str
    ) -> None:
        async with client(await runtime(engine, uow)) as c:
            bodies = [(await c.get(path, headers=AUTH)).text for path in READS]

        for body in bodies:
            assert API_TOKEN not in body
            assert database_url not in body


class TestOperatorActions:
    async def test_trip_is_audited_with_the_operator(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        rt = await runtime(engine, uow, trade=False)
        async with client(rt) as c:
            response = await c.post(
                "/kill-switch/trip",
                json={"operator": "bijesh", "reason": "investigating odd fills"},
                headers=AUTH,
            )

        assert response.status_code == 200
        assert response.json()["kill_switch"]["state"] == "tripped"
        assert response.json()["kill_switch"]["actor"] == "api:bijesh"
        async with client(rt) as c:
            assert (await c.get("/ready", headers=AUTH)).status_code == 503
        state = await rt.session.kill_switch.status()
        assert state is not None
        assert state.state is KillSwitchState.TRIPPED
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT payload->>'actor', payload->>'reason' FROM system_events "
                        "WHERE event_type = 'kill_switch.tripped'"
                    )
                )
            ).one()
        assert tuple(row) == ("api:bijesh", "investigating odd fills")
        assert rt.metrics.operator_actions.labels("kill_switch_trip")._value.get() == 1  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.parametrize(
        "body",
        [
            {"operator": "", "reason": "x"},
            {"operator": "has space", "reason": "x"},
            {"operator": "ok", "reason": ""},
            {"operator": "ok", "reason": "   "},
            {"operator": "ok", "reason": "x" * 501},
            {"operator": "ok", "reason": "x", "arm": True},
        ],
    )
    async def test_trip_requires_operator_and_reason(
        self, engine: AsyncEngine, uow: UowFactory, body: dict[str, object]
    ) -> None:
        async with client(await runtime(engine, uow, trade=False)) as c:
            response = await c.post("/kill-switch/trip", json=body, headers=AUTH)

        assert response.status_code == 422
        assert await kill_switch_state(engine) == "armed"

    async def test_reconciliation_request_runs_and_is_audited(
        self, engine: AsyncEngine, uow: UowFactory
    ) -> None:
        rt = await runtime(engine, uow)
        async with client(rt) as c:
            response = await c.post("/reconciliation", json={"operator": "bijesh"}, headers=AUTH)

        assert response.status_code == 200
        assert response.json()["reconciliation"]["clean"] is True
        assert rt.session.last_reconciliation is not None
        async with engine.connect() as conn:
            actor = await conn.scalar(
                text(
                    "SELECT payload->>'actor' FROM system_events "
                    "WHERE event_type = 'operator.reconciliation_requested'"
                )
            )
        assert actor == "api:bijesh"
