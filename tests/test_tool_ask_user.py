"""`ask_user` tool — durable pause/resume orchestration, DB + channel stubbed.

Covers: requires ctx.task_id, input validation, delivery via the live stream
(web emit) vs a proactive channel push (Telegram), answer return, and timeout.
The DB layer (`pending_questions` DAO) and channel delivery are monkeypatched
so this stays a fast unit test; the real LISTEN/NOTIFY wait is exercised by the
DB-backed suite."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from wolfpaw.memory.pending_questions import AnswerOutcome
from wolfpaw.toolbox.registry import (
    ToolContext,
    ToolError,
    get_registry as get_tool_registry,
)


@asynccontextmanager
async def _fake_acquire():
    yield None


@pytest.fixture
def stub(monkeypatch):
    """Stub the DB writes + the durable wait. Returns a control object the
    test tunes (the answer to hand back) and inspects (what got recorded)."""
    state = SimpleNamespace(
        transitions=[],
        created=None,
        answer_to_return=SimpleNamespace(status="answered", answer="yes"),
        wait_returns_none=False,
    )

    async def fake_create(_conn, *, task_id, user_id, question, thread_id=None,
                          channel=None, options=None, urgency="normal",
                          timeout_seconds=300):
        state.created = SimpleNamespace(
            id=uuid4(), question=question, options=options, channel=channel,
            timeout_seconds=timeout_seconds,
        )
        return state.created

    async def fake_wait(*, question_id, timeout):
        return None if state.wait_returns_none else state.answer_to_return

    async def fake_mark_awaiting(_conn, *, task_id, reason=None):
        state.transitions.append("awaiting_user")

    async def fake_mark_started(_conn, *, task_id):
        state.transitions.append("running")

    async def fake_mark_blocked(_conn, *, task_id, reason):
        state.transitions.append("blocked")

    async def fake_mark_timeout(_conn, *, question_id):
        state.transitions.append("q_timeout")

    async def fake_append_event(_conn, *, task_id, event_type, content=None):
        state.transitions.append(f"event:{event_type}")
        return uuid4()

    monkeypatch.setattr("wolfpaw.toolbox.tools.ask_user.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.toolbox.tools.ask_user.pq_dao.create", fake_create)
    monkeypatch.setattr("wolfpaw.toolbox.tools.ask_user.pq_dao.wait_for_answer", fake_wait)
    monkeypatch.setattr("wolfpaw.toolbox.tools.ask_user.pq_dao.mark_timeout", fake_mark_timeout)
    monkeypatch.setattr(
        "wolfpaw.toolbox.tools.ask_user.tasks_dao.mark_awaiting_user", fake_mark_awaiting)
    monkeypatch.setattr(
        "wolfpaw.toolbox.tools.ask_user.tasks_dao.mark_started", fake_mark_started)
    monkeypatch.setattr(
        "wolfpaw.toolbox.tools.ask_user.tasks_dao.mark_blocked", fake_mark_blocked)
    monkeypatch.setattr(
        "wolfpaw.toolbox.tools.ask_user.task_events.append_event", fake_append_event)
    return state


# --- validation (no DB needed) ---------------------------------------------


async def test_requires_task_id():
    tool = get_tool_registry().get("ask_user")
    ctx = ToolContext(user_id=uuid4())  # no task_id
    with pytest.raises(ToolError, match="Task context"):
        await tool.run(ctx, question="should I proceed?")


async def test_requires_nonempty_question():
    tool = get_tool_registry().get("ask_user")
    ctx = ToolContext(user_id=uuid4(), task_id=uuid4())
    with pytest.raises(ToolError, match="question"):
        await tool.run(ctx, question="   ")


async def test_options_must_be_list():
    tool = get_tool_registry().get("ask_user")
    ctx = ToolContext(user_id=uuid4(), task_id=uuid4())
    with pytest.raises(ToolError, match="options"):
        await tool.run(ctx, question="?", options="yes")


# --- delivery + resume ------------------------------------------------------


async def test_emits_to_live_stream_when_present(stub):
    """Web path: a question with an open stream is emitted as an `ask_user`
    event (not pushed via a channel)."""
    tool = get_tool_registry().get("ask_user")
    emitted: list[tuple[str, str]] = []

    async def emit(event, data):
        emitted.append((event, data))

    ctx = ToolContext(user_id=uuid4(), task_id=uuid4(), channel="web", emit=emit)
    result = await tool.run(
        ctx, question="overwrite notes.md?", options=["yes", "no"])

    assert emitted and emitted[0][0] == "ask_user"
    payload = json.loads(emitted[0][1])
    assert payload["question"] == "overwrite notes.md?"
    assert payload["options"] == ["yes", "no"]
    assert result["answer"] == "yes"
    assert "awaiting_user" in stub.transitions
    assert "event:user_question" in stub.transitions
    assert "running" in stub.transitions
    assert "event:user_answer" in stub.transitions


async def test_pushes_via_channel_when_no_stream(stub, monkeypatch):
    """Telegram path: no emit → the question is pushed through the channel's
    send(), and the options are rendered into the message text."""
    tool = get_tool_registry().get("ask_user")
    sent: list[tuple] = []

    class FakeChannel:
        async def send(self, user_id, content, **kwargs):
            sent.append((user_id, content))

    monkeypatch.setattr(
        "wolfpaw.toolbox.tools.ask_user.get_channel", lambda name: FakeChannel())

    ctx = ToolContext(user_id=uuid4(), task_id=uuid4(), channel="telegram")
    result = await tool.run(ctx, question="ship it?", options=["yes", "no"])

    assert len(sent) == 1
    _, content = sent[0]
    assert "ship it?" in content
    assert "1. yes" in content and "2. no" in content
    assert result["answer"] == "yes"


async def test_returns_the_answer(stub):
    tool = get_tool_registry().get("ask_user")
    stub.answer_to_return = SimpleNamespace(status="answered", answer="the moon")
    ctx = ToolContext(user_id=uuid4(), task_id=uuid4(), channel="web",
                      emit=_noop_emit)
    result = await tool.run(ctx, question="where to?")
    assert result["answer"] == "the moon"


async def test_timeout_marks_blocked_and_raises(stub):
    tool = get_tool_registry().get("ask_user")
    stub.wait_returns_none = True
    ctx = ToolContext(user_id=uuid4(), task_id=uuid4(), channel="web",
                      emit=_noop_emit)
    with pytest.raises(ToolError, match="timed out"):
        await tool.run(ctx, question="still there?", timeout_seconds=1)
    assert "q_timeout" in stub.transitions
    assert "blocked" in stub.transitions


async def _noop_emit(event, data):
    return None
