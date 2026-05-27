"""Enqueue abstraction shared by every caller that wants to defer work.

Each public ``enqueue_*`` helper either:

- Pushes the job onto arq when ``WOLFPAW_WORKERS_ENABLED=true``. The
  worker (``wolfpaw.workers.arq_app``) picks it up from Redis and
  executes against a fresh process, so the job survives a restart of
  the FastAPI app.
- Or fires the same coroutine via ``asyncio.create_task`` inside the
  caller's event loop. This is the single-process dev default —
  Redis isn't required, the chat path works the same as before step
  23, and the post-append follow-ups stay attached to the request's
  event loop.

The arq side addresses jobs by name (``WorkerSettings.functions``
registers them). Each typed enqueue helper here pairs with a job
function in ``wolfpaw.workers.jobs`` that the worker imports.

Lifecycle:

- :func:`get_pool` lazily creates the arq Redis pool on first use and
  caches it. :func:`close_pool` is wired into the FastAPI lifespan
  hook so the connection drains on shutdown.
- The inline-fallback path keeps a strong reference to spawned tasks
  via a module-level set so the event loop doesn't GC them mid-flight.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from wolfpaw.config import get_settings
from wolfpaw.tracing import get_logger

log = get_logger()

_pool: Any | None = None
_pool_lock = asyncio.Lock()
_inline_tasks: set[asyncio.Task] = set()


async def get_pool():
    """Return the process-wide arq Redis pool, creating it on first
    call. Raises if workers are disabled — callers should branch on
    ``get_settings().workers_enabled`` before invoking."""
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is not None:
            return _pool
        settings = get_settings()
        if not settings.workers_enabled:
            raise RuntimeError(
                "queue.get_pool called while WOLFPAW_WORKERS_ENABLED=false"
            )
        # Lazy import — arq pulls in aioredis, which we don't want to
        # load in tests or the inline-fallback path.
        from arq import create_pool
        from arq.connections import RedisSettings

        log.info("workers.queue.pool.init", redis_url=_redact(settings.redis_url))
        _pool = await create_pool(
            RedisSettings.from_dsn(settings.redis_url),
        )
        return _pool


async def close_pool() -> None:
    """Drain the pool on shutdown. Safe to call when the pool was
    never created."""
    global _pool
    if _pool is None:
        return
    try:
        await _pool.close()
    except Exception:  # noqa: BLE001
        log.warning("workers.queue.pool.close_failed", exc_info=True)
    _pool = None


def _spawn_inline(coro) -> None:
    """Fire-and-forget on the current loop, with a strong reference so
    the task survives until completion."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — the caller is in a sync context. Drop the
        # work rather than create a new loop (which would leak).
        log.warning("workers.queue.inline.no_loop")
        coro.close()
        return
    task = loop.create_task(coro)
    _inline_tasks.add(task)
    task.add_done_callback(_inline_tasks.discard)


# --- public enqueue helpers ------------------------------------------------


async def enqueue_compact_thread(thread_id: UUID) -> None:
    """Trigger one drain pass over the thread's compaction backlog.
    Called from ``conv.append`` post-write."""
    from wolfpaw.workers.jobs.compact_thread import compact_thread

    settings = get_settings()
    if not settings.workers_enabled:
        _spawn_inline(compact_thread(thread_id))
        return
    pool = await get_pool()
    await pool.enqueue_job("compact_thread_job", str(thread_id))


async def enqueue_embed_message(
    thread_id: UUID, message_id: UUID, content: str
) -> None:
    """Embed a newly-appended message and write the row into
    ``message_embeddings``. Called from ``conv.append`` post-write."""
    from wolfpaw.memory.conversational import embed_and_store

    settings = get_settings()
    if not settings.workers_enabled:
        _spawn_inline(embed_and_store(message_id, content))
        return
    pool = await get_pool()
    await pool.enqueue_job(
        "embed_message_job", str(thread_id), str(message_id), content,
    )


async def enqueue_embed_workspace_file(file_id: UUID, text: str) -> None:
    """Embed a newly-written workspace file so it shows up in
    `search_docs`. Called from ``write_doc`` post-register."""
    from wolfpaw.workspace.files import embed_and_store_doc

    settings = get_settings()
    if not settings.workers_enabled:
        _spawn_inline(embed_and_store_doc(file_id, text))
        return
    pool = await get_pool()
    await pool.enqueue_job(
        "embed_workspace_file_job", str(file_id), text,
    )


async def enqueue_run_task(task_id: UUID) -> None:
    """Run a queued Task end-to-end (planner → executor → post-eval +
    terminal transition). Called from the Router's task path when
    workers are enabled."""
    from wolfpaw.tasks.service import get_task_service

    settings = get_settings()
    if not settings.workers_enabled:
        _spawn_inline(get_task_service().run(task_id))
        return
    pool = await get_pool()
    await pool.enqueue_job("run_task_job", str(task_id))


async def enqueue_telegram_dispatch(
    *, user_id: UUID, tg_chat_id: str, content: str,
) -> None:
    """Drive the Router for a free-form Telegram message and push the
    reply via the Telegram Bot API. Replaces the fire-and-forget
    ``asyncio.create_task`` in the webhook handler so a server restart
    mid-reply doesn't leave the user hanging."""
    from wolfpaw.channels.telegram import _handle_inbound

    settings = get_settings()
    if not settings.workers_enabled:
        _spawn_inline(
            _handle_inbound(
                user_id=user_id, tg_chat_id=tg_chat_id, content=content,
            )
        )
        return
    pool = await get_pool()
    await pool.enqueue_job(
        "telegram_dispatch_job", str(user_id), tg_chat_id, content,
    )


async def enqueue_sleep_cycle() -> None:
    """Manually trigger the Sleep Cycle (step 26). The weekly cron in
    ``arq_app.py`` is what normally fires this; this helper is for
    operators who want an ad-hoc run (e.g. after a Post-Evaluator
    prompt change) or for tests."""
    from wolfpaw.workers.jobs.sleep_cycle import sleep_cycle

    settings = get_settings()
    if not settings.workers_enabled:
        _spawn_inline(sleep_cycle())
        return
    pool = await get_pool()
    await pool.enqueue_job("sleep_cycle_job")


async def enqueue_slack_dispatch(
    *,
    user_id: UUID,
    workspace_bot_token: str,
    channel_id: str,
    content: str,
) -> None:
    """Drive the Router for a Slack inbound (slash command OR DM) and
    push the reply via ``chat.postMessage``. Both Slack entry points
    (events API for DMs, slash command for ``/wolfpaw <text>``) share
    the same downstream flow — Slack's 3-second ack window is what
    forces both to defer the actual agent work.

    The bot token is passed in so the worker doesn't have to re-fetch
    the workspace row on each invocation; the webhook already loaded
    it to authorize the ack."""
    from wolfpaw.channels.slack import _handle_freeform

    settings = get_settings()
    if not settings.workers_enabled:
        _spawn_inline(
            _handle_freeform(
                user_id=user_id,
                workspace_bot_token=workspace_bot_token,
                channel_id=channel_id, content=content,
            )
        )
        return
    pool = await get_pool()
    await pool.enqueue_job(
        "slack_dispatch_job",
        str(user_id), workspace_bot_token, channel_id, content,
    )


# --- helpers ---------------------------------------------------------------


def _redact(url: str) -> str:
    import re

    return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", url)
