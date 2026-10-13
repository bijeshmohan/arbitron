"""The operational API (plan §16; operator decisions 2026-10-13).

Bound to ``127.0.0.1`` on the host and reached over an SSH tunnel. Every
endpoint except ``GET /health`` needs ``Authorization: Bearer <token>``.

Read: ``/ready``, ``/metrics``, ``/status``, ``/portfolio``, ``/positions``,
``/orders``, ``/signals``, ``/risk/decisions``, ``/system/events``.

Operator actions (audited; they can only make the system safer):
``POST /kill-switch/trip`` and ``POST /reconciliation``. Review, re-arm, and
order resolution are CLI-only.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST
from pydantic import BaseModel, ConfigDict, Field

from arbitron.api import views
from arbitron.api.auth import bearer_auth
from arbitron.application.operations import MAX_REASON_LENGTH, OperatorActions
from arbitron.domain.portfolio import Balance
from arbitron.monitoring.health import HealthRegistry, Readiness
from arbitron.monitoring.metrics import Metrics
from arbitron.ports.repositories import ConcurrencyConflict, UnitOfWork

DEFAULT_LIMIT = 50
MAX_LIMIT = 500
_OPERATOR = r"^[A-Za-z0-9_.@-]{1,64}$"

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]


@dataclass(frozen=True, slots=True)
class ApiContext:
    health: HealthRegistry
    readiness: Callable[[], Awaitable[Readiness]]
    metrics: Metrics
    token: str
    uow_factory: Callable[[], UnitOfWork]
    balances: Callable[[], Awaitable[Mapping[str, Balance]]]
    operator: OperatorActions
    mode: str
    exchange_id: str
    symbols: tuple[str, ...]
    quote_currency: str


class TripRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operator: str = Field(pattern=_OPERATOR)
    reason: str = Field(min_length=1, max_length=MAX_REASON_LENGTH)


class ReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operator: str = Field(pattern=_OPERATOR)


def create_app(ctx: ApiContext) -> FastAPI:
    app = FastAPI(title="arbitron", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def get_health() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        # Unauthenticated liveness probe for the container runtime: no details.
        return JSONResponse({"status": "alive"})

    auth = bearer_auth(ctx.token, on_failure=ctx.metrics.api_auth_failures.inc)
    api = APIRouter(dependencies=[Depends(auth)])
    _add_reads(api, ctx)
    _add_actions(api, ctx)
    app.include_router(api)
    return app


def _add_reads(api: APIRouter, ctx: ApiContext) -> None:
    @api.get("/ready")
    async def get_ready() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        result = await ctx.readiness()
        return JSONResponse(
            {"ready": result.ready, "checks": dict(result.checks)},
            status_code=200 if result.ready else 503,
        )

    @api.get("/metrics")
    async def get_metrics() -> Response:  # pyright: ignore[reportUnusedFunction]
        return Response(ctx.metrics.render(), media_type=CONTENT_TYPE_LATEST)

    @api.get("/status")
    async def get_status() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        readiness = await ctx.readiness()
        async with ctx.uow_factory() as tx:
            kill_switch = await tx.kill_switch.get()
            latest = await tx.equity_snapshots.latest(ctx.quote_currency)
            peak = await tx.equity_snapshots.peak(ctx.quote_currency)
        return JSONResponse(
            {
                "mode": ctx.mode,
                "simulated": ctx.mode != "live",
                "exchange": ctx.exchange_id,
                "symbols": list(ctx.symbols),
                "ready": readiness.ready,
                "checks": dict(readiness.checks),
                "kill_switch": views.kill_switch(kill_switch),
                "equity": views.equity(latest, peak),
                "tasks": ctx.health.view(),
            }
        )

    @api.get("/portfolio")
    async def get_portfolio() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        balances = await ctx.balances()
        async with ctx.uow_factory() as tx:
            positions = await tx.positions.list(ctx.exchange_id)
            latest = await tx.equity_snapshots.latest(ctx.quote_currency)
            peak = await tx.equity_snapshots.peak(ctx.quote_currency)
        return JSONResponse(
            {
                "simulated": ctx.mode != "live",
                "balances": views.balances(balances),
                "positions": [views.position(p) for p in positions],
                "equity": views.equity(latest, peak),
            }
        )

    @api.get("/positions")
    async def get_positions() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        async with ctx.uow_factory() as tx:
            positions = await tx.positions.list(ctx.exchange_id)
        return JSONResponse({"positions": [views.position(p) for p in positions]})

    @api.get("/orders")
    async def get_orders(limit: Limit = DEFAULT_LIMIT) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        async with ctx.uow_factory() as tx:
            orders = await tx.orders.list_recent(ctx.exchange_id, limit)
        return JSONResponse({"orders": [views.order(o) for o in orders]})

    @api.get("/signals")
    async def get_signals(limit: Limit = DEFAULT_LIMIT) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        async with ctx.uow_factory() as tx:
            signals = await tx.signals.list_recent(ctx.exchange_id, limit)
        return JSONResponse({"signals": [views.signal(s) for s in signals]})

    @api.get("/risk/decisions")
    async def get_risk_decisions(limit: Limit = DEFAULT_LIMIT) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        async with ctx.uow_factory() as tx:
            decisions = await tx.risk_decisions.list_recent(limit)
        return JSONResponse({"decisions": [views.risk_decision(d) for d in decisions]})

    @api.get("/system/events")
    async def get_system_events(limit: Limit = DEFAULT_LIMIT) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        async with ctx.uow_factory() as tx:
            events = await tx.system_events.list_recent(limit)
        return JSONResponse({"events": [views.system_event(e) for e in events]})


def _add_actions(api: APIRouter, ctx: ApiContext) -> None:
    @api.post("/kill-switch/trip")
    async def trip(body: TripRequest) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        try:
            state = await ctx.operator.trip_kill_switch(operator=body.operator, reason=body.reason)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        except ConcurrencyConflict as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "kill switch changed; retry") from exc
        ctx.metrics.operator_actions.labels("kill_switch_trip").inc()
        return JSONResponse({"kill_switch": views.kill_switch(state)})

    @api.post("/reconciliation")
    async def reconcile(body: ReconcileRequest) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        try:
            report = await ctx.operator.request_reconciliation(operator=body.operator)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        ctx.metrics.operator_actions.labels("reconciliation").inc()
        return JSONResponse({"reconciliation": views.reconciliation(report)})
