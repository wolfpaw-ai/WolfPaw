"""FastAPI application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from wolfpaw import __version__
from wolfpaw.auth.routes import router as auth_router
from wolfpaw.channels.slack import router as slack_channel_router
from wolfpaw.channels.telegram import router as telegram_channel_router
from wolfpaw.channels.web import router as web_channel_router
from wolfpaw.config import get_settings
from wolfpaw.integrations import dropbox as _dropbox_integration  # noqa: F401 — registers Dropbox tools
from wolfpaw.integrations import microsoft as _microsoft_integration  # noqa: F401 — registers Outlook tools
from wolfpaw.integrations import notion as _notion_integration  # noqa: F401 — registers Notion tools
from wolfpaw.integrations.dropbox.routes import router as dropbox_router
from wolfpaw.integrations.microsoft.routes import router as microsoft_router
from wolfpaw.integrations.notion.routes import router as notion_router
from wolfpaw.memory.db import close_pool
from wolfpaw.workers.queue import close_pool as close_queue_pool
from wolfpaw.metering import usage_report as _usage_report  # noqa: F401 — registers /usage
from wolfpaw.metering.routes import router as usage_router
from wolfpaw import toolbox as _toolbox  # noqa: F401 — registers v1 tool set
from wolfpaw.persona.routes import router as persona_router
from wolfpaw.tasks import commands as _task_commands  # noqa: F401 — registers /tasks etc
from wolfpaw.tasks.routes import router as tasks_router
from wolfpaw.workspace.routes import router as workspace_router
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
    await close_queue_pool()
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
    app.include_router(web_channel_router)
    app.include_router(telegram_channel_router)
    app.include_router(slack_channel_router)
    app.include_router(workspace_router)
    app.include_router(persona_router)
    app.include_router(tasks_router)
    app.include_router(usage_router)
    app.include_router(dropbox_router)
    app.include_router(notion_router)
    app.include_router(microsoft_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "env": settings.env}

    return app


app = create_app()
