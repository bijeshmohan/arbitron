"""Read-only operational endpoints: ``/health``, ``/ready``, ``/metrics`` (plan §16).

Bound to ``127.0.0.1`` by default. Nothing here can change trading state;
authenticated operator actions arrive in Phase 10.

- ``/health``: liveness. 200 while the process and supervisor are running,
  with per-task heartbeat details.
- ``/ready``: 200 only when it is safe to trade (startup done, kill switch
  armed, reconciliation clean, market data ok, critical tasks beating);
  otherwise 503 with the failing checks.
- ``/metrics``: Prometheus exposition.
"""

import asyncio
from collections.abc import Awaitable, Callable

import uvicorn
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST

from arbitron.monitoring.health import HealthRegistry, Readiness
from arbitron.monitoring.metrics import Metrics


def create_app(
    *,
    health: HealthRegistry,
    readiness: Callable[[], Awaitable[Readiness]],
    metrics: Metrics,
) -> FastAPI:
    app = FastAPI(title="arbitron", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def get_health() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return JSONResponse({"status": "alive", "tasks": health.view()})

    @app.get("/ready")
    async def get_ready() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        result = await readiness()
        return JSONResponse(
            {"ready": result.ready, "checks": dict(result.checks)},
            status_code=200 if result.ready else 503,
        )

    @app.get("/metrics")
    async def get_metrics() -> Response:  # pyright: ignore[reportUnusedFunction]
        return Response(metrics.render(), media_type=CONTENT_TYPE_LATEST)

    return app


class HttpServerError(RuntimeError):
    """The endpoint server could not start or stopped unexpectedly."""


async def serve(app: FastAPI, *, host: str, port: int, stop: asyncio.Event) -> None:
    """Serve until ``stop`` is set, then shut down gracefully.

    uvicorn calls ``sys.exit`` when it cannot bind (e.g. the port is busy).
    That ``SystemExit`` is converted to ``HttpServerError`` so the supervisor
    treats it as an ordinary crash of this non-critical task, instead of it
    escaping and taking the trading process down.
    """
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_config=None, access_log=False, lifespan="off")
    )

    async def guarded() -> None:
        # Must be caught inside the task: asyncio re-raises a task's SystemExit
        # straight out of the event loop instead of storing it on the task.
        try:
            await server.serve()
        except SystemExit as exc:
            msg = f"HTTP server could not start on {host}:{port} (port in use?)"
            raise HttpServerError(msg) from exc

    serving = asyncio.create_task(guarded())
    stopping = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait({serving, stopping}, return_when=asyncio.FIRST_COMPLETED)
    server.should_exit = True
    if serving in done:
        stopping.cancel()
        await serving  # surfaces a startup failure such as a busy port
        msg = f"HTTP server on {host}:{port} stopped unexpectedly"
        raise HttpServerError(msg)
    await serving
