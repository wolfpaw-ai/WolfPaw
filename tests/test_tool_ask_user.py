"""`ask_user` tool — pause/resume flow with stubbed DB writes.

Covers: requires ctx.task_id, registers a question, awaits + returns the
answer, times out cleanly, surfaces input validation."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from wolfpaw.tasks.ask_user_registry import AskUserRegistry, get_registry
from wolfpaw.toolbox.registry import ToolContext, ToolError, get_registry as get_tool_registry


@asynccontextmanager
async def _fake_acquire():
    yield None


@pytest.fixture(autouse=True)
def _stub_db(monkeypatch):
    """Stub out the DB writes the tool does on pause / resume / timeout."""
    transitions: list[str] = []

    async def fake_mark_awaiting(_conn, *, task_id, reason=None):
        transitions.append("awaiting_user")
        return None

    async def fake_mark_started(_conn, *, task_id):
        transitions.append("running")
        return None

    async def fake_mark_blocked(_conn, *, task_id, reason):
        transitions.append("blocked")
        return None

    async def fake_append_event(_conn, *, task_id, event_type, content=None):
        transitions.append(f"event:{event_type}")
        return uuid4()

    monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.toolbox.tools.ask_user.acquire", _fake_acquire)
    monkeypatch.setattr(
        "wolfpaw.toolbox.tools.ask_user.tasks_dao.mark_awaiting_user",
        fake_mark_awaiting,
    )
    monkeypatch.setattr(
        "wolfpaw.toolbox.tools.ask_user.tasks_dao.mark_started",
        fake_mark_started,
    )
    monkeypatch.setattr(
        "wolfpaw.toolbox.tools.ask_user.tasks_dao.mark_blocked",
        fake_mark_blocked,
    )
    monkeypatch.setattr(
        "wolfpaw.toolbox.tools.ask_user.task_events.append_event",
        fake_append_event,
    )

    yield transitions


@pytest.fixture
def fresh_registry(monkeypatch):
    """Each test gets its own AskUserRegistry so cross-test pollution
    can't happen."""
    reg = AskUserRegistry()
    monkeypatch.setattr(
        "wolfpaw.toolbox.tools.ask_user.get_registry", lambda: reg,
    )
    return reg


# --- tests -----------------------------------------------------------------


async def test_requires_task_id():
    tool = get_tool_registry().get("ask_user")
    ctx = ToolContext(user_id=uuid4())  # no task_id
    with pytest.raises(ToolError, match="Task context"):
        await tool.run(ctx, question="should I proceed?")


async def test_requires_nonempty_question(fresh_registry):
    tool = get_tool_registry().get("ask_user")
    ctx = ToolContext(user_id=uuid4(), task_id=uuid4())
    with pytest.raises(ToolError, match="question"):
        await tool.run(ctx, question="   ")


async def test_options_must_be_list(fresh_registry):
    tool = get_tool_registry().get("ask_user")
    ctx = ToolContext(user_id=uuid4(), task_id=uuid4())
    with pytest.raises(ToolError, match="options"):
        await tool.run(ctx, question="?", options="yes")


async def test_pause_then_resume_returns_answer(_stub_db, fresh_registry):
    tool = get_tool_registry().get("ask_user")
    uid = uuid4()
    ctx = ToolContext(user_id=uid, task_id=uuid4())

    # Kick off the tool call.
    tool_task = asyncio.create_task(tool.run(ctx, question="overwrite?"))
    # Give the tool a chance to register the question.
    for _ in range(20):
        await asyncio.sleep(0)
        if fresh_registry._pending:  # type: ignore[attr-defined]
            break
    assert fresh_registry._pending, "tool didn't register a question"  # type: ignore[attr-defined]
    question_id = next(iter(fresh_registry._pending))  # type: ignore[attr-defined]

    # Submit the answer.
    await fresh_registry.submit_answer(
        question_id=question_id, user_id=uid, answer="yes",
    )
    result = await tool_task
    assert result == {"question_id": str(question_id), "answer": "yes"}
    # Transitions hit: awaiting_user → user_question event → running → user_answer event.
    assert "awaiting_user" in _stub_db
    assert "event:user_question" in _stub_db
    assert "running" in _stub_db
    assert "event:user_answer" in _stub_db


async def test_emits_ask_user_event_before_awaiting(_stub_db, fresh_registry):
    """The outbound leg: the tool must push an `ask_user` event to the
    channel so the client learns a question is pending. Without this the
    question is invisible and the await below just hangs to timeout —
    the original bug."""
    import json

    tool = get_tool_registry().get("ask_user")
    uid = uuid4()
    emitted: list[tuple[str, str]] = []

    async def emit(event, data):
        emitted.append((event, data))

    ctx = ToolContext(user_id=uid, task_id=uuid4(), emit=emit)
    tool_task = asyncio.create_task(
        tool.run(ctx, question="overwrite notes.md?", options=["yes", "no"])
    )
    for _ in range(20):
        await asyncio.sleep(0)
        if emitted:
            break
    assert emitted, "tool never emitted the question to the channel"
    event, data = emitted[0]
    assert event == "ask_user"
    payload = json.loads(data)
    question_id = next(iter(fresh_registry._pending))  # type: ignore[attr-defined]
    assert payload["question_id"] == str(question_id)
    assert payload["question"] == "overwrite notes.md?"
    assert payload["options"] == ["yes", "no"]

    # Resolve so the awaiting task doesn't leak.
    await fresh_registry.submit_answer(
        question_id=question_id, user_id=uid, answer="yes",
    )
    await tool_task


async def test_timeout_marks_blocked_and_raises(_stub_db, fresh_registry):
    tool = get_tool_registry().get("ask_user")
    ctx = ToolContext(user_id=uuid4(), task_id=uuid4())
    with pytest.raises(ToolError, match="timed out"):
        # 1-second timeout, no answer submitted.
        await tool.run(ctx, question="?", timeout_seconds=1)
    assert "blocked" in _stub_db
    assert "event:user_question_timeout" in _stub_db
