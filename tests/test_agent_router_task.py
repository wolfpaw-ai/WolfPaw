"""Router tests for the `task` verdict — TaskService invocation,
title derivation, persistence, fallthrough behavior."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from wolfpaw.agents.router import Router, _title_from_content
from wolfpaw.agents.triage import TriageVerdict
from wolfpaw.memory.tasks import Task
from wolfpaw.tasks.service import TaskOutcome
from wolfpaw.toolbox.registry import ToolContext


@asynccontextmanager
async def _fake_acquire():
    yield None


@pytest.fixture(autouse=True)
def _stub_persistence(monkeypatch):
    """conv.append goes to an in-memory list."""
    appended: list[dict] = []

    async def fake_append(_conn, **kw):
        appended.append(kw)
        return uuid4()

    monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.agents.router.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.agents.router.conv.append", fake_append)
    yield appended


@dataclass
class FakeTriage:
    verdict: TriageVerdict

    async def classify(self, *, ctx, thread_id, content):
        return self.verdict


class FakeTaskService:
    def __init__(self, *, final_answer: str = "done."):
        self.calls: list[dict] = []
        self._final = final_answer

    async def create_and_run(
        self, *, user_id, thread_id, content, title,
        description=None, channel_for_completion=None,
        complexity_hint="moderate", emit=None,
    ):
        self.calls.append({
            "user_id": user_id, "thread_id": thread_id, "content": content,
            "title": title, "description": description,
            "channel_for_completion": channel_for_completion,
            "complexity_hint": complexity_hint, "emit_is_none": emit is None,
        })
        if emit is not None:
            await emit("task", "fake-task-id")
        fake_task = Task(
            id=uuid4(), user_id=user_id, parent_task_id=None,
            title=title, description=description, status="completed",
            current_plan_id=None, budget_cents=None, spent_cents=0,
            blocking_reason=None, channel_for_completion=channel_for_completion,
            schedule_pattern=None, created_at=datetime.now(timezone.utc),
            started_at=None, completed_at=None, last_active_at=None,
        )
        return TaskOutcome(
            task=fake_task, plan=None, execution=None, verdict=None,
            final_answer=self._final,
        )


def _ctx():
    return ToolContext(user_id=uuid4())


# --- helpers --------------------------------------------------------------


def test_title_from_content_uses_first_line():
    assert _title_from_content("research vendors\n\ndetails here") == "research vendors"


def test_title_from_content_caps_long_first_line():
    long = "x" * 200
    title = _title_from_content(long)
    assert len(title) <= 80
    assert title.endswith("…")


def test_title_from_content_empty_falls_back():
    assert _title_from_content("   ") == "Task"


# --- router task path -----------------------------------------------------


async def test_task_verdict_invokes_task_service():
    svc = FakeTaskService(final_answer="task done.")
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="task", complexity="ambitious", reasoning="r"),
        ),
        task_service=svc,
    )
    text = await router.handle(
        ctx=_ctx(), thread_id=uuid4(),
        content="monitor stock XYZ and alert me when it crosses $100",
    )
    assert text == "task done."
    assert len(svc.calls) == 1
    call = svc.calls[0]
    assert call["complexity_hint"] == "ambitious"
    assert call["title"] == "monitor stock XYZ and alert me when it crosses $100"
    assert call["channel_for_completion"] == "web"


async def test_task_path_persists_user_and_final_answer(_stub_persistence):
    appended = _stub_persistence
    svc = FakeTaskService(final_answer="result")
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="task", complexity="moderate", reasoning="r"),
        ),
        task_service=svc,
    )
    await router.handle(ctx=_ctx(), thread_id=uuid4(), content="watch X")
    roles = [a["role"] for a in appended]
    contents = [a["content"] for a in appended]
    assert roles == ["user", "assistant"]
    assert contents == ["watch X", "result"]


async def test_task_path_propagates_emit_callback():
    emitted: list[tuple[str, str]] = []

    async def emit(event, data):
        emitted.append((event, data))

    svc = FakeTaskService()
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="task", complexity="moderate", reasoning="r"),
        ),
        task_service=svc,
    )
    await router.handle(ctx=_ctx(), thread_id=uuid4(), content="x", emit=emit)
    kinds = [e for e, _ in emitted]
    assert "triage" in kinds
    # FakeTaskService emits a "task" event when given an emit callback.
    assert "task" in kinds


# --- workers-enabled path (step 23) ---------------------------------------


class _DeferredFakeTaskService:
    """Workers-on flow drives only `create()` from the Router; the
    actual `run` happens on the arq worker. This fake records the
    create call and produces a Task row stub."""

    def __init__(self) -> None:
        self.create_calls: list[dict] = []
        self.run_calls: list = []

    async def create(self, **kw):
        self.create_calls.append(kw)
        return Task(
            id=uuid4(), user_id=kw["user_id"], parent_task_id=None,
            title=kw["title"], description=kw.get("description"),
            status="pending", current_plan_id=None, budget_cents=None,
            spent_cents=0, blocking_reason=None,
            channel_for_completion=kw.get("channel_for_completion"),
            schedule_pattern=None, created_at=datetime.now(timezone.utc),
            started_at=None, completed_at=None, last_active_at=None,
        )

    async def run(self, task_id):
        # Should NOT be called from the Router when workers are on —
        # the arq worker owns this path. Record so we can assert.
        self.run_calls.append(task_id)


async def test_workers_on_uses_create_plus_enqueue(monkeypatch, _stub_persistence):
    """With WOLFPAW_WORKERS_ENABLED=true, the Router calls
    task_service.create() (not create_and_run) and enqueues the run,
    then returns an acknowledgment message. The arq worker handles
    the actual run."""
    from wolfpaw.config import get_settings

    monkeypatch.setenv("WOLFPAW_WORKERS_ENABLED", "true")
    get_settings.cache_clear()  # type: ignore[attr-defined]

    enqueued: list = []

    async def fake_enqueue_run_task(task_id):
        enqueued.append(task_id)

    monkeypatch.setattr(
        "wolfpaw.workers.queue.enqueue_run_task", fake_enqueue_run_task,
    )

    svc = _DeferredFakeTaskService()
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="task", complexity="moderate", reasoning="r"),
        ),
        task_service=svc,  # type: ignore[arg-type]
    )
    text = await router.handle(
        ctx=_ctx(), thread_id=uuid4(), content="monitor X long-term",
    )
    assert len(svc.create_calls) == 1
    assert svc.run_calls == []  # run does NOT happen in the router
    assert len(enqueued) == 1
    # The user sees an ack pointing at the new task id.
    assert "Started Task" in text
    assert str(enqueued[0]) in text


async def test_workers_off_uses_create_and_run_inline(monkeypatch, _stub_persistence):
    """Default (workers off): the Router uses create_and_run as before
    and never touches the queue."""
    from wolfpaw.config import get_settings

    monkeypatch.setenv("WOLFPAW_WORKERS_ENABLED", "false")
    get_settings.cache_clear()  # type: ignore[attr-defined]

    enqueued: list = []

    async def fake_enqueue_run_task(task_id):
        enqueued.append(task_id)

    monkeypatch.setattr(
        "wolfpaw.workers.queue.enqueue_run_task", fake_enqueue_run_task,
    )

    svc = FakeTaskService(final_answer="done")
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="task", complexity="moderate", reasoning="r"),
        ),
        task_service=svc,
    )
    text = await router.handle(
        ctx=_ctx(), thread_id=uuid4(), content="short task",
    )
    assert text == "done"
    assert len(svc.calls) == 1
    assert enqueued == []
