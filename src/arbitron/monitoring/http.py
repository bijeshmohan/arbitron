"""Serving the operational API with uvicorn (the app itself is ``arbitron.api.app``)."""

import asyncio

import uvicorn
from fastapi import FastAPI


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
