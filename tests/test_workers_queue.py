"""Tests for the workers queue's enqueue helpers.

Each ``enqueue_*`` helper branches on ``WOLFPAW_WORKERS_ENABLED``:

- Workers off → spawns the inline coroutine via ``asyncio.create_task``;
  the wrapped handler is called with the right arguments and runs in
  the same event loop.
- Workers on → routes through ``get_pool().enqueue_job(...)`` against a
  named arq job. We mock the pool to confirm the function name + args.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from wolfpaw.workers import queue as queue_mod


@pytest.fixture(autouse=True)
def _reset_pool(monkeypatch):
    """The pool is module-global; clear it between tests so a Fake
    pool from one test doesn't leak into the next."""
    monkeypatch.setattr(queue_mod, "_pool", None, raising=False)
    queue_mod._inline_tasks.clear()
    yield
    monkeypatch.setattr(queue_mod, "_pool", None, raising=False)
    queue_mod._inline_tasks.clear()


class _FakePool:
    """Stand-in for arq's Redis pool — records every enqueue_job call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def enqueue_job(self, function_name: str, *args, **kwargs):
        self.calls.append(
            {"function_name": function_name, "args": list(args),
             "kwargs": kwargs}
        )
        return SimpleNamespace(job_id="fake")

    async def close(self) -> None:
        pass


def _enable_workers(monkeypatch, *, fake_pool=None):
    """Flip workers_enabled to True and swap get_pool for a fake."""
    from wolfpaw.config import get_settings

    monkeypatch.setenv("WOLFPAW_WORKERS_ENABLED", "true")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    pool = fake_pool or _FakePool()

    async def fake_get_pool():
        return pool

    monkeypatch.setattr(queue_mod, "get_pool", fake_get_pool)
    return pool


def _disable_workers(monkeypatch):
    from wolfpaw.config import get_settings

    monkeypatch.setenv("WOLFPAW_WORKERS_ENABLED", "false")
    get_settings.cache_clear()  # type: ignore[attr-defined]


# --- compact_thread + embed_message ---------------------------------------


async def test_enqueue_compact_thread_inline_when_workers_off(monkeypatch):
    _disable_workers(monkeypatch)
    called_with: list = []

    async def fake_compact(thread_id):
        called_with.append(thread_id)

    monkeypatch.setattr(
        "wolfpaw.workers.jobs.compact_thread.compact_thread", fake_compact,
    )

    tid = uuid4()
    await queue_mod.enqueue_compact_thread(tid)
    # The inline task is fire-and-forget — await one loop tick to let
    # it run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert called_with == [tid]


async def test_enqueue_compact_thread_uses_arq_when_workers_on(monkeypatch):
    pool = _enable_workers(monkeypatch)
    tid = uuid4()
    await queue_mod.enqueue_compact_thread(tid)
    assert pool.calls == [
        {"function_name": "compact_thread_job",
         "args": [str(tid)], "kwargs": {}}
    ]


async def test_enqueue_embed_message_inline_when_workers_off(monkeypatch):
    _disable_workers(monkeypatch)
    captured: list = []

    async def fake_embed(message_id, content):
        captured.append((message_id, content))

    monkeypatch.setattr(
        "wolfpaw.memory.conversational.embed_and_store", fake_embed,
    )

    tid, mid = uuid4(), uuid4()
    await queue_mod.enqueue_embed_message(tid, mid, "hello")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert captured == [(mid, "hello")]


async def test_enqueue_embed_message_uses_arq_when_workers_on(monkeypatch):
    pool = _enable_workers(monkeypatch)
    tid, mid = uuid4(), uuid4()
    await queue_mod.enqueue_embed_message(tid, mid, "hi")
    assert pool.calls == [
        {"function_name": "embed_message_job",
         "args": [str(tid), str(mid), "hi"], "kwargs": {}}
    ]


# --- run_task -------------------------------------------------------------


async def test_enqueue_run_task_inline_when_workers_off(monkeypatch):
    _disable_workers(monkeypatch)
    called_with: list = []

    async def fake_run(task_id):
        called_with.append(task_id)

    fake_svc = SimpleNamespace(run=fake_run)
    monkeypatch.setattr(
        "wolfpaw.tasks.service.get_task_service", lambda: fake_svc,
    )

    task_id = uuid4()
    await queue_mod.enqueue_run_task(task_id)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert called_with == [task_id]


async def test_enqueue_run_task_uses_arq_when_workers_on(monkeypatch):
    pool = _enable_workers(monkeypatch)
    task_id = uuid4()
    await queue_mod.enqueue_run_task(task_id)
    assert pool.calls == [
        {"function_name": "run_task_job",
         "args": [str(task_id)], "kwargs": {}}
    ]


# --- sleep cycle ----------------------------------------------------------


async def test_enqueue_sleep_cycle_inline_when_workers_off(monkeypatch):
    _disable_workers(monkeypatch)
    called: list = []

    async def fake_run():
        called.append("ran")

    monkeypatch.setattr(
        "wolfpaw.workers.jobs.sleep_cycle.sleep_cycle", fake_run,
    )

    await queue_mod.enqueue_sleep_cycle()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert called == ["ran"]


async def test_enqueue_sleep_cycle_uses_arq_when_workers_on(monkeypatch):
    pool = _enable_workers(monkeypatch)
    await queue_mod.enqueue_sleep_cycle()
    assert pool.calls == [
        {"function_name": "sleep_cycle_job", "args": [], "kwargs": {}}
    ]


# --- telegram + slack -----------------------------------------------------


async def test_enqueue_telegram_dispatch_inline_when_workers_off(monkeypatch):
    _disable_workers(monkeypatch)
    captured: list = []

    async def fake_handle(*, user_id, tg_chat_id, content):
        captured.append((user_id, tg_chat_id, content))

    monkeypatch.setattr(
        "wolfpaw.channels.telegram._handle_inbound", fake_handle,
    )

    uid = uuid4()
    await queue_mod.enqueue_telegram_dispatch(
        user_id=uid, tg_chat_id="42", content="hi",
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert captured == [(uid, "42", "hi")]


async def test_enqueue_telegram_dispatch_uses_arq_when_workers_on(monkeypatch):
    pool = _enable_workers(monkeypatch)
    uid = uuid4()
    await queue_mod.enqueue_telegram_dispatch(
        user_id=uid, tg_chat_id="42", content="hi",
    )
    assert pool.calls == [
        {"function_name": "telegram_dispatch_job",
         "args": [str(uid), "42", "hi"], "kwargs": {}}
    ]


async def test_enqueue_slack_dispatch_inline_when_workers_off(monkeypatch):
    _disable_workers(monkeypatch)
    captured: list = []

    async def fake_handle(*, user_id, workspace_bot_token, channel_id, content):
        captured.append((user_id, workspace_bot_token, channel_id, content))

    monkeypatch.setattr(
        "wolfpaw.channels.slack._handle_freeform", fake_handle,
    )

    uid = uuid4()
    await queue_mod.enqueue_slack_dispatch(
        user_id=uid, workspace_bot_token="xoxb-x",
        channel_id="C123", content="hello",
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert captured == [(uid, "xoxb-x", "C123", "hello")]


async def test_enqueue_slack_dispatch_uses_arq_when_workers_on(monkeypatch):
    pool = _enable_workers(monkeypatch)
    uid = uuid4()
    await queue_mod.enqueue_slack_dispatch(
        user_id=uid, workspace_bot_token="xoxb-x",
        channel_id="C123", content="hello",
    )
    assert pool.calls == [
        {"function_name": "slack_dispatch_job",
         "args": [str(uid), "xoxb-x", "C123", "hello"], "kwargs": {}}
    ]


# --- pool lifecycle -------------------------------------------------------


async def test_get_pool_raises_when_workers_disabled(monkeypatch):
    _disable_workers(monkeypatch)
    with pytest.raises(RuntimeError, match="WOLFPAW_WORKERS_ENABLED=false"):
        await queue_mod.get_pool()


async def test_close_pool_is_safe_without_init(monkeypatch):
    """close_pool can be called even when the pool was never opened —
    important for the api.py lifespan hook on workers-off deployments."""
    _disable_workers(monkeypatch)
    await queue_mod.close_pool()  # must not raise


async def test_close_pool_drains_initialized_pool(monkeypatch):
    pool = _enable_workers(monkeypatch)
    # Stash the pool so the queue module thinks it's initialized.
    monkeypatch.setattr(queue_mod, "_pool", pool, raising=False)
    pool.close = AsyncMock()
    await queue_mod.close_pool()
    pool.close.assert_awaited_once()
    assert queue_mod._pool is None
