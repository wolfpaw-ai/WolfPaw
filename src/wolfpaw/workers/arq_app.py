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
- ``embed_workspace_file_job`` — write a vector onto a ``workspace_files`` row.
- ``run_task_job`` — drive a queued Task end-to-end.
- ``telegram_dispatch_job`` — handle a Telegram free-form inbound.
- ``slack_dispatch_job`` — handle a Slack DM or slash command inbound.
- ``sleep_cycle_job`` — periodic memory maintenance (re-score plans,
  consolidate skills, GC orphan threads). Registered as both a function
  (manual enqueue for ad-hoc runs) AND a cron job firing Sunday 03:00
  UTC. The job itself no-ops unless ``WOLFPAW_SLEEP_CYCLE_ENABLED=true``,
  so the cron schedule is safe to keep registered on every deployment.
- ``dispatch_schedules_job`` — per-minute schedule dispatcher: claims due
  ``schedules`` rows and spawns a Task per schedule. No-ops unless
  ``WOLFPAW_SCHEDULES_ENABLED=true``; safe to keep registered everywhere.
- ``prune_traces_job`` — daily ``model_call_logs`` partition maintenance:
  provision next month, drop partitions past the retention window.

Adding a new job: define it in ``workers/jobs/`` (taking ``_ctx, ...``
as the arq signature), import it here, and append to
``WorkerSettings.functions``. The matching ``enqueue_*`` helper in
:mod:`wolfpaw.workers.queue` is what FastAPI-side callers use.
"""

from __future__ import annotations

from arq import cron
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
    embed_workspace_file_job,
)
from wolfpaw.workers.jobs.dispatch_schedules import dispatch_schedules_job
from wolfpaw.workers.jobs.prune_traces import prune_traces_job
from wolfpaw.workers.jobs.run_task import run_task_job
from wolfpaw.workers.jobs.sleep_cycle import sleep_cycle_job

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
        embed_workspace_file_job,
        run_task_job,
        telegram_dispatch_job,
        slack_dispatch_job,
        sleep_cycle_job,
        dispatch_schedules_job,
        prune_traces_job,
    ]
    cron_jobs = [
        # Weekly Sleep Cycle — Sunday 03:00 UTC. The job itself gates on
        # WOLFPAW_SLEEP_CYCLE_ENABLED so this entry is safe on every
        # deployment; flipping the env var enables the work without
        # changing the worker config.
        cron(
            sleep_cycle_job,
            name="sleep_cycle",
            weekday="sun",
            hour=3,
            minute=0,
        ),
        # Schedule dispatcher — every minute, at second 0. Claims due
        # `schedules` rows and spawns Tasks. No-ops unless
        # WOLFPAW_SCHEDULES_ENABLED=true, so safe to keep registered
        # everywhere; per-minute is the granularity floor for scheduled
        # tasks (see schedules_min_interval_seconds).
        cron(
            dispatch_schedules_job,
            name="dispatch_schedules",
            second=0,
        ),
        # Trace retention — daily at 04:00 UTC. Provisions next month's
        # `model_call_logs` partition and drops any whose range has aged out
        # past WOLFPAW_TRACE_RETENTION_DAYS. Idempotent, so a missed run (or
        # several) self-corrects on the next tick — but note that partitions
        # are provisioned only a month ahead, so a worker down for weeks
        # across a month boundary will start failing trace writes (the model
        # calls themselves are unaffected).
        cron(
            prune_traces_job,
            name="prune_traces",
            hour=4,
            minute=0,
        ),
    ]
    on_startup = _on_startup
    on_shutdown = _on_shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
