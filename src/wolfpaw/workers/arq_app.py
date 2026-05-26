"""arq worker entrypoint.

Run with ``arq wolfpaw.workers.arq_app.WorkerSettings`` (or via the
docker-compose ``worker`` service). The worker pulls jobs off Redis
that callers in the FastAPI process enqueued via
:mod:`wolfpaw.workers.queue`, and runs them against the same
Postgres / Anthropic / Voyage stack.

Lifecycle hooks initialize the asyncpg pool on startup and drain it
on shutdown so the worker doesn't leak DB connections across reloads.

Job registration:

- ``compact_thread_job`` — tiered conversational memory compaction.
- ``embed_message_job`` — write a vector into ``message_embeddings``.
- ``run_task_job`` — drive a queued Task end-to-end.
- ``telegram_dispatch_job`` — handle a Telegram free-form inbound.
- ``slack_dispatch_job`` — handle a Slack DM or slash command inbound.

Adding a new job: define it in ``workers/jobs/`` (taking ``_ctx, ...``
as the arq signature), import it here, and append to
``WorkerSettings.functions``. The matching ``enqueue_*`` helper in
:mod:`wolfpaw.workers.queue` is what FastAPI-side callers use.
"""

from __future__ import annotations

from arq.connections import RedisSettings

from wolfpaw.config import get_settings
from wolfpaw.memory.db import close_pool, get_pool
from wolfpaw.tracing import get_logger
from wolfpaw.workers.jobs.channel_dispatch import (
    slack_dispatch_job,
    telegram_dispatch_job,
)
from wolfpaw.workers.jobs.compact_thread import (
    compact_thread_job,
    embed_message_job,
)
from wolfpaw.workers.jobs.run_task import run_task_job

log = get_logger()


async def _on_startup(ctx: dict) -> None:
    """Warm the asyncpg pool so the first job doesn't pay the cold-
    start cost. arq passes the worker context dict for us to stash
    long-lived resources on (we just use the global pool)."""
    log.info("workers.arq.startup")
    await get_pool()


async def _on_shutdown(_ctx: dict) -> None:
    """Drain the asyncpg pool on graceful shutdown."""
    log.info("workers.arq.shutdown")
    await close_pool()


class WorkerSettings:
    """arq picks this class up by name (``arq wolfpaw.workers.arq_app.WorkerSettings``)."""

    functions = [
        compact_thread_job,
        embed_message_job,
        run_task_job,
        telegram_dispatch_job,
        slack_dispatch_job,
    ]
    on_startup = _on_startup
    on_shutdown = _on_shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
