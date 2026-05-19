"""FastAPI application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from wolfpaw import __version__
from wolfpaw.auth.routes import router as auth_router
from wolfpaw.config import get_settings
from wolfpaw.memory.db import close_pool
from wolfpaw.tracing import (
    configure_logging,
    get_logger,
    new_trace_id,
    set_trace_id,
)


class TraceIdMiddleware(BaseHTTPMiddleware):
    """Resolve an inbound `x-trace-id` (or mint one) and stamp it on every log."""

    async def dispatch(self, request: Request, call_next) -> Response:
        log = get_logger()
        incoming = request.headers.get("x-trace-id")
        trace_id = incoming or new_trace_id()
        set_trace_id(trace_id)
        log.info(
            "request.start",
            method=request.method,
            path=request.url.path,
        )
        try:
            response = await call_next(request)
        except Exception:
            log.exception("request.error")
            raise
        response.headers["x-trace-id"] = trace_id
        log.info(
            "request.end",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
        )
        set_trace_id(None)
        return response


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    await close_pool()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="Wolfpaw",
        version=__version__,
        description="A careful, capable, general-purpose AI worker.",
        lifespan=_lifespan,
    )
    app.add_middleware(TraceIdMiddleware)
    app.include_router(auth_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "env": settings.env}

    return app


app = create_app()
