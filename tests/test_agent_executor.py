"""Executor tests — step dispatch, parallel groups, failure handling,
synthesis short-circuit, sandbox teardown, persistence."""

from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Iterable
from uuid import UUID, uuid4

import pytest

from wolfpaw.agents.executor import ExecutorAgent
from wolfpaw.metering.model_client import ModelClient
from wolfpaw.schemas import (
    ExecutionPlan, Plan, Step, StepResult, StepStatus,
)
from wolfpaw.toolbox.registry import Registry, Tool, ToolContext, ToolError


@asynccontextmanager
async def _fake_acquire():
    yield None


# --- fakes -----------------------------------------------------------------


class FakeAnthropic:
    def __init__(self, replies: Iterable[str]) -> None:
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    async def create(self, **kwargs):
        snapshot = dict(kwargs)
        if "messages" in snapshot:
            snapshot["messages"] = copy.deepcopy(snapshot["messages"])
        self.calls.append(snapshot)
        text = self._replies.pop(0) if self._replies else "(no reply queued)"
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=10, output_tokens=5,
                cache_read_input_tokens=0, cache_creation_input_tokens=0,
            ),
        )


class _Recorder:
    """Tool that records every call and returns its (canned) output."""

    def __init__(self, name: str, output: Any = None, error: str | None = None,
                 *, delay: float = 0.0):
        self.name = name
        self.description = f"recorder/{name}"
        self.input_schema = {"type": "object", "properties": {}}
        self._output = output if output is not None else {"name": name, "ok": True}
        self._error = error
        self._delay = delay
        self.calls: list[dict] = []

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict:
        self.calls.append({"inputs": inputs, "user_id": ctx.user_id,
                           "at": asyncio.get_event_loop().time()})
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise ToolError(self._error)
        return self._output


@dataclass
class _Persisted:
    update_calls: list[dict] = field(default_factory=list)
    sandbox_closes: list[tuple[UUID, UUID | None]] = field(default_factory=list)


@pytest.fixture
def exec_env(monkeypatch):
    persisted = _Persisted()

    async def fake_update_outcome(
        _conn, *, plan_id, final_answer=None, success=None,
        score=None, error=None,
    ):
        persisted.update_calls.append({
            "plan_id": plan_id, "final_answer": final_answer,
            "success": success, "score": score, "error": error,
        })

    async def fake_bump(_conn, *, agent, version_label, content_template):
        return SimpleNamespace(
            id=uuid4(), agent=agent, version_label=version_label,
            content_hash="fake", content_template=content_template,
        )

    async def fake_record(*args, **kwargs):
        return uuid4()

    async def fake_price(*args, **kwargs):
        return None

    class _SandboxStub:
        async def close_for_task(self, user_id, task_id):
            persisted.sandbox_closes.append((user_id, task_id))

    monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.agents.executor.acquire", _fake_acquire)
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.acquire", _fake_acquire,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.executor.procedural.update_outcome", fake_update_outcome,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.executor.bump_prompt_version", fake_bump,
    )
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.record_usage", fake_record,
    )
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.get_active_price", fake_price,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.executor.get_sandbox_manager", lambda: _SandboxStub(),
    )
    yield persisted


def _plan(steps: list[Step], *, plan_id: UUID | None = None, query: str = "q") -> Plan:
    return Plan(
        query=query, summary="run it", steps=steps,
        id=plan_id, model_used="claude-sonnet-4-6",
    )


def _registry(*tools) -> Registry:
    r = Registry()
    for t in tools:
        r.register(t)
    return r


def _ctx():
    return ToolContext(user_id=uuid4(), task_id=uuid4())


# --- tests -----------------------------------------------------------------


async def test_single_functional_step_then_synthesis(exec_env):
    rec = _Recorder("web_search", output={"results": ["a", "b"]})
    fake = FakeAnthropic(replies=["Found 2 results: a and b."])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake),
        registry=_registry(rec),
    )
    plan = _plan(
        [Step(id="search", kind="functional", description="search",
              tool="web_search", inputs={"query": "x"})],
        plan_id=uuid4(),
    )
    execution = await executor.execute(ctx=_ctx(), plan=plan)
    assert execution.success is True
    assert "Found 2" in execution.final_answer
    assert len(rec.calls) == 1
    # One synthesis call (no reasoning step at the end → fall through to synth).
    assert len(fake.calls) == 1
    # Persisted to procedural memory with no score.
    assert len(exec_env.update_calls) == 1
    assert exec_env.update_calls[0]["success"] is True
    assert exec_env.update_calls[0]["score"] is None


async def test_last_reasoning_step_skips_synthesis_call(exec_env):
    rec = _Recorder("web_search", output={"results": ["x"]})
    # Only one model call expected — the reasoning step itself, no synth.
    fake = FakeAnthropic(replies=["Final answer text."])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake),
        registry=_registry(rec),
    )
    plan = _plan(
        [
            Step(id="s1", kind="functional", description="search",
                 tool="web_search"),
            Step(id="s2", kind="reasoning", description="summarize"),
        ],
        plan_id=uuid4(),
    )
    execution = await executor.execute(ctx=_ctx(), plan=plan)
    assert execution.success is True
    assert execution.final_answer == "Final answer text."
    assert len(fake.calls) == 1  # reasoning only, no extra synthesis


async def test_step_failure_skips_remaining_and_returns_error_summary(exec_env):
    good = _Recorder("good", output={"ok": True})
    bad = _Recorder("bad", error="downstream blew up")
    fake = FakeAnthropic(replies=[])  # no model calls should happen
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake),
        registry=_registry(good, bad),
    )
    plan = _plan(
        [
            Step(id="s1", kind="functional", tool="good", description="ok"),
            Step(id="s2", kind="functional", tool="bad", description="bad"),
            Step(id="s3", kind="functional", tool="good", description="never runs"),
        ],
        plan_id=uuid4(),
    )
    execution = await executor.execute(ctx=_ctx(), plan=plan)
    assert execution.success is False
    assert execution.error == "downstream blew up"
    assert "s2" in execution.final_answer
    statuses = {r.step_id: r.status for r in execution.results}
    assert statuses["s1"] == StepStatus.COMPLETED
    assert statuses["s2"] == StepStatus.FAILED
    assert statuses["s3"] == StepStatus.SKIPPED
    # Persistence still happens, marking failure.
    assert exec_env.update_calls[-1]["success"] is False


async def test_parallel_group_runs_concurrently(exec_env):
    fetch1 = _Recorder("fetch1", delay=0.10)
    fetch2 = _Recorder("fetch2", delay=0.10)
    fake = FakeAnthropic(replies=["synth"])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake),
        registry=_registry(fetch1, fetch2),
    )
    plan = _plan(
        [
            Step(id="a", kind="functional", tool="fetch1",
                 description="fetch a", parallel_group=1),
            Step(id="b", kind="functional", tool="fetch2",
                 description="fetch b", parallel_group=1),
        ],
        plan_id=uuid4(),
    )
    import time
    start = time.monotonic()
    execution = await executor.execute(ctx=_ctx(), plan=plan)
    elapsed = time.monotonic() - start
    assert execution.success is True
    # Two 100ms sleeps run in parallel → well under 200ms total.
    assert elapsed < 0.18, f"parallel steps not concurrent (took {elapsed:.2f}s)"


async def test_sequential_steps_run_serially(exec_env):
    fetch1 = _Recorder("fetch1", delay=0.05)
    fetch2 = _Recorder("fetch2", delay=0.05)
    fake = FakeAnthropic(replies=["synth"])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake),
        registry=_registry(fetch1, fetch2),
    )
    plan = _plan(
        [
            Step(id="a", kind="functional", tool="fetch1", description="a"),
            Step(id="b", kind="functional", tool="fetch2", description="b"),
        ],
        plan_id=uuid4(),
    )
    # No assertion on wall time here — just that both ran and recorded
    # their call timestamps in the order they were dispatched.
    await executor.execute(ctx=_ctx(), plan=plan)
    assert fetch1.calls[0]["at"] < fetch2.calls[0]["at"]


async def test_unknown_tool_marks_step_failed(exec_env):
    fake = FakeAnthropic(replies=[])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake),
        registry=_registry(),  # empty
    )
    plan = _plan([
        Step(id="x", kind="functional", tool="not_a_tool", description="x")
    ])
    execution = await executor.execute(ctx=_ctx(), plan=plan)
    assert execution.success is False
    assert "not_a_tool" in execution.results[0].error


async def test_sandbox_close_for_task_runs_in_finally(exec_env):
    """Even if a step fails, the sandbox is torn down."""
    bad = _Recorder("bad", error="boom")
    fake = FakeAnthropic(replies=[])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake),
        registry=_registry(bad),
    )
    ctx = _ctx()
    plan = _plan([
        Step(id="x", kind="functional", tool="bad", description="x")
    ])
    await executor.execute(ctx=ctx, plan=plan)
    assert exec_env.sandbox_closes == [(ctx.user_id, ctx.task_id)]


async def test_step_emits_start_end_events(exec_env):
    rec = _Recorder("t", output={"ok": True})
    fake = FakeAnthropic(replies=["synth"])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake),
        registry=_registry(rec),
    )
    plan = _plan([Step(id="s1", kind="functional", tool="t", description="d")])

    events: list[tuple[str, str]] = []

    async def emit(event, data):
        events.append((event, data))

    await executor.execute(ctx=_ctx(), plan=plan, emit=emit)
    kinds = [e for e, _ in events]
    assert "step.start" in kinds
    assert "step.end" in kinds


async def test_step_error_emits_step_error_event(exec_env):
    bad = _Recorder("bad", error="oops")
    fake = FakeAnthropic(replies=[])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake),
        registry=_registry(bad),
    )
    plan = _plan([Step(id="x", kind="functional", tool="bad", description="x")])

    events: list[tuple[str, str]] = []

    async def emit(event, data):
        events.append((event, data))

    await executor.execute(ctx=_ctx(), plan=plan, emit=emit)
    kinds = [e for e, _ in events]
    assert "step.error" in kinds


async def test_plan_without_id_skips_persistence(exec_env):
    """If the planner failed to persist + the plan has no id, the
    executor shouldn't attempt update_outcome."""
    rec = _Recorder("t", output={"ok": True})
    fake = FakeAnthropic(replies=["synth"])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake),
        registry=_registry(rec),
    )
    plan = _plan(
        [Step(id="s1", kind="functional", tool="t", description="d")],
        plan_id=None,
    )
    await executor.execute(ctx=_ctx(), plan=plan)
    assert exec_env.update_calls == []


async def test_step_cap_rejects_oversized_plans(exec_env):
    fake = FakeAnthropic(replies=[])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake),
        registry=_registry(),
    )
    big_plan = _plan(
        [Step(id=f"s{i}", kind="reasoning", description="x") for i in range(100)],
        plan_id=uuid4(),
    )
    execution = await executor.execute(ctx=_ctx(), plan=big_plan)
    assert execution.success is False
    assert execution.error == "step_cap_exceeded"


# --- WorkspaceCollision → ask_user hook ---------------------------------


class _CollidingWriteDoc:
    """Stand-in for the real write_doc tool: raises WorkspaceCollision on
    the first call when `overwrite` is not set, succeeds on retry."""

    def __init__(self, *, existing_filename: str = "report.md",
                 existing_version: int = 1):
        from datetime import datetime, timezone
        from wolfpaw.workspace.files import WorkspaceFile

        self.name = "write_doc"
        self.description = "fake write_doc"
        self.input_schema = {"type": "object", "properties": {}}
        self._existing = WorkspaceFile(
            id=uuid4(), user_id=uuid4(), task_id=None, source="agent_output",
            filename=existing_filename, mime_type="text/markdown",
            storage_url="local://x", size_bytes=1, version=existing_version,
            supersedes_id=None, sha256=None,
            created_at=datetime.now(timezone.utc),
        )
        self.calls: list[dict[str, Any]] = []

    async def run(self, ctx, **inputs):
        self.calls.append(dict(inputs))
        if not inputs.get("overwrite"):
            from wolfpaw.workspace.files import WorkspaceCollision
            raise WorkspaceCollision(self._existing)
        return {
            "file_id": str(uuid4()),
            "filename": inputs["filename"],
            "version": self._existing.version + 1,
            "size_bytes": len(inputs.get("content", "")),
            "sha256": None,
        }


class _FakeAskUser:
    """Stand-in for the ask_user tool; replies with a canned answer."""

    def __init__(self, answer: str):
        self.name = "ask_user"
        self.description = "fake ask_user"
        self.input_schema = {"type": "object", "properties": {}}
        self._answer = answer
        self.calls: list[dict[str, Any]] = []

    async def run(self, ctx, **inputs):
        self.calls.append(dict(inputs))
        return {"question_id": str(uuid4()), "answer": self._answer}


async def test_workspace_collision_with_yes_retries_with_overwrite(exec_env):
    """write_doc raises WorkspaceCollision → executor calls ask_user → on
    "yes" the step retries with overwrite=True and succeeds."""
    writer = _CollidingWriteDoc()
    asker = _FakeAskUser(answer="yes")
    fake = FakeAnthropic(replies=["synth"])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake),
        registry=_registry(writer, asker),
    )
    plan = _plan(
        [Step(id="w", kind="functional", tool="write_doc",
              description="save report",
              inputs={"filename": "report.md", "content": "hello"})],
        plan_id=uuid4(),
    )
    execution = await executor.execute(ctx=_ctx(), plan=plan)
    assert execution.success is True
    # First call: no overwrite (raised). Second call: overwrite=True.
    assert len(writer.calls) == 2
    assert writer.calls[0].get("overwrite") in (None, False)
    assert writer.calls[1]["overwrite"] is True
    # ask_user was prompted once with the filename in the question.
    assert len(asker.calls) == 1
    assert "report.md" in asker.calls[0]["question"]


async def test_workspace_collision_with_no_propagates_failure(exec_env):
    writer = _CollidingWriteDoc()
    asker = _FakeAskUser(answer="no")
    fake = FakeAnthropic(replies=[])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake),
        registry=_registry(writer, asker),
    )
    plan = _plan(
        [Step(id="w", kind="functional", tool="write_doc",
              description="save report",
              inputs={"filename": "report.md", "content": "hello"})],
        plan_id=uuid4(),
    )
    execution = await executor.execute(ctx=_ctx(), plan=plan)
    assert execution.success is False
    # The collision message bubbles into the step error.
    failed = next(r for r in execution.results if r.status == StepStatus.FAILED)
    assert "report.md" in failed.error
    # Asked once, no retry.
    assert len(asker.calls) == 1
    assert len(writer.calls) == 1


async def test_workspace_collision_without_task_skips_ask_user(exec_env):
    """Without a Task context, ask_user can't be used. The collision
    surfaces as a normal step failure — same v1 behavior as before."""
    writer = _CollidingWriteDoc()
    asker = _FakeAskUser(answer="yes")  # shouldn't be called
    fake = FakeAnthropic(replies=[])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake),
        registry=_registry(writer, asker),
    )
    plan = _plan(
        [Step(id="w", kind="functional", tool="write_doc",
              description="save",
              inputs={"filename": "x.md", "content": "y"})],
        plan_id=uuid4(),
    )
    ctx = ToolContext(user_id=uuid4(), task_id=None)
    execution = await executor.execute(ctx=ctx, plan=plan)
    assert execution.success is False
    assert len(asker.calls) == 0
    assert len(writer.calls) == 1
