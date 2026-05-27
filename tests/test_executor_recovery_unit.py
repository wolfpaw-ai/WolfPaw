"""Step-level retry on ToolError (feature 1).

When a functional step's tool raises ToolError, the Executor calls a
cheap model to repair the inputs and runs the tool again. Bounded to
``_MAX_TOOL_ATTEMPTS`` (= initial + 1 retry).
"""

from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Iterable
from uuid import UUID, uuid4

import pytest

from wolfpaw.agents.executor import ExecutorAgent
from wolfpaw.metering.model_client import ModelClient
from wolfpaw.schemas import Plan, Step, StepStatus
from wolfpaw.toolbox.registry import Registry, ToolContext, ToolError


@asynccontextmanager
async def _fake_acquire():
    yield None


class FakeAnthropic:
    """Scriptable Anthropic stub that returns either text (synthesis) or
    a tool_use block (the recover model call). Replies are popped in
    order; each reply is either a string (→ text block) or a
    ``{"tool": name, "input": dict}`` (→ tool_use block)."""

    def __init__(self, replies: Iterable[Any]) -> None:
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    async def create(self, **kwargs):
        snapshot = dict(kwargs)
        if "messages" in snapshot:
            snapshot["messages"] = copy.deepcopy(snapshot["messages"])
        self.calls.append(snapshot)
        reply = self._replies.pop(0) if self._replies else "(no reply queued)"
        if isinstance(reply, dict) and "tool" in reply:
            content = [SimpleNamespace(
                type="tool_use", name=reply["tool"],
                input=reply.get("input", {}), id="tu_" + reply["tool"],
            )]
            stop_reason = "tool_use"
        else:
            content = [SimpleNamespace(type="text", text=str(reply))]
            stop_reason = "end_turn"
        return SimpleNamespace(
            content=content, stop_reason=stop_reason,
            usage=SimpleNamespace(
                input_tokens=10, output_tokens=5,
                cache_read_input_tokens=0, cache_creation_input_tokens=0,
            ),
        )


class _SeqTool:
    """Tool whose run() consumes a queue of outcomes. Each outcome is
    either a dict (returned as output) or a ToolError (raised). Lets
    one test drive 'first call fails, retry succeeds' end-to-end."""

    def __init__(self, name: str, outcomes: Iterable[Any]):
        self.name = name
        self.description = f"seq/{name}"
        self.input_schema = {"type": "object", "properties": {}}
        self._outcomes = list(outcomes)
        self.calls: list[dict] = []

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict:
        self.calls.append(dict(inputs))
        outcome = (
            self._outcomes.pop(0) if self._outcomes
            else {"name": self.name, "ok": True}
        )
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@dataclass
class _Persisted:
    update_calls: list[dict] = field(default_factory=list)


@pytest.fixture
def exec_env(monkeypatch):
    persisted = _Persisted()

    async def fake_update_outcome(_conn, **kw):
        persisted.update_calls.append(kw)

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
        async def close_for_task(self, *args, **kwargs):
            pass

    async def fake_find_user_tool(_conn, *, user_id, name):
        return None

    import wolfpaw.memory.tools as _tools_dao

    monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.agents.executor.acquire", _fake_acquire)
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.acquire", _fake_acquire,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.executor.procedural.update_outcome",
        fake_update_outcome,
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
    monkeypatch.setattr(
        _tools_dao, "find_active_by_name", fake_find_user_tool,
    )
    yield persisted


def _plan(steps: list[Step], *, plan_id: UUID | None = None) -> Plan:
    return Plan(
        query="q", summary="run", steps=steps,
        id=plan_id, model_used="claude-sonnet-4-6",
    )


def _registry(*tools) -> Registry:
    r = Registry()
    for t in tools:
        r.register(t)
    return r


def _ctx() -> ToolContext:
    return ToolContext(user_id=uuid4(), task_id=uuid4())


# --- tests -----------------------------------------------------------------


async def test_recover_succeeds_when_model_repairs_inputs(exec_env):
    """The recipes-bug reproduction: tool fails with a 'column does not
    exist' style error, model proposes corrected inputs, second tool
    call succeeds. Step ends COMPLETED, no replan happens."""
    seq_tool = _SeqTool(
        "sql_insert",
        outcomes=[
            ToolError(
                "column 'title' of relation 'recipes' does not exist —"
                " actual columns on 'recipes': [id, vendor, amount]"
            ),
            {"inserted": 1, "schema": "u_xxx", "table": "recipes"},
        ],
    )
    fake = FakeAnthropic(replies=[
        # 1. recover model call — propose corrected inputs (drop "title")
        {"tool": "propose_inputs", "input": {
            "reasoning": "drop the invalid 'title' field",
            "corrected_inputs": {
                "table_name": "recipes",
                "rows": [{"id": 1, "vendor": "Acme", "amount": 9.99}],
            },
        }},
        # 2. final synthesis call after the step succeeds
        "Inserted 1 row.",
    ])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake),
        registry=_registry(seq_tool),
    )
    plan = _plan([
        Step(
            id="ins", kind="functional",
            description="insert meatloaf",
            tool="sql_insert",
            inputs={
                "table_name": "recipes",
                "rows": [{"id": 1, "title": "Meatloaf"}],
            },
        ),
    ], plan_id=uuid4())

    execution = await executor.execute(ctx=_ctx(), plan=plan)

    assert execution.success is True
    assert execution.results[0].status == StepStatus.COMPLETED
    # Tool was called twice (initial + retry).
    assert len(seq_tool.calls) == 2
    # The retry used the corrected inputs (no "title" key).
    assert "title" not in seq_tool.calls[1]["rows"][0]
    # Two model calls total: recover + synthesis.
    assert len(fake.calls) == 2


async def test_recover_gives_up_when_model_returns_give_up(exec_env):
    """Model says give_up=true → no retry, step fails."""
    seq_tool = _SeqTool(
        "sql_insert",
        outcomes=[ToolError("column 'title' does not exist")],
    )
    fake = FakeAnthropic(replies=[
        {"tool": "propose_inputs", "input": {
            "reasoning": "can't infer correct column without more info",
            "give_up": True,
        }},
    ])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake),
        registry=_registry(seq_tool),
        # No Planner — replan would be a no-op because we'd need one.
        # The post-failure path doesn't reach replan in this test
        # because we want to confirm 'give up' short-circuits at (1).
        planner=_StubReplanPlanner(continuation=None),
    )
    plan = _plan([
        Step(
            id="ins", kind="functional", description="insert",
            tool="sql_insert", inputs={"x": 1},
        ),
    ], plan_id=uuid4())

    execution = await executor.execute(ctx=_ctx(), plan=plan)

    assert execution.success is False
    assert execution.results[0].status == StepStatus.FAILED
    # Only the initial call ran — no retry attempt.
    assert len(seq_tool.calls) == 1


async def test_recover_exhausts_when_retry_also_fails(exec_env):
    """Model proposes corrected inputs, but the second tool call also
    fails. _MAX_TOOL_ATTEMPTS = 2, so no third try; step FAILED."""
    seq_tool = _SeqTool(
        "sql_insert",
        outcomes=[
            ToolError("first failure"),
            ToolError("second failure"),
        ],
    )
    fake = FakeAnthropic(replies=[
        {"tool": "propose_inputs", "input": {
            "reasoning": "try this",
            "corrected_inputs": {"x": 1},
        }},
    ])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake),
        registry=_registry(seq_tool),
        planner=_StubReplanPlanner(continuation=None),
    )
    plan = _plan([
        Step(
            id="ins", kind="functional", description="insert",
            tool="sql_insert", inputs={"x": 0},
        ),
    ], plan_id=uuid4())

    execution = await executor.execute(ctx=_ctx(), plan=plan)

    assert execution.success is False
    assert execution.results[0].status == StepStatus.FAILED
    assert "second failure" in (execution.results[0].error or "")
    # Bounded: exactly 2 attempts, no third.
    assert len(seq_tool.calls) == 2


class _StubReplanPlanner:
    """Replan-only Planner stub: returns a fixed continuation or None.
    The recover tests pass continuation=None so a replan after step-
    level retry exhaustion can't accidentally rescue the test."""

    def __init__(self, *, continuation):
        self.continuation = continuation
        self.replan_calls: list[dict] = []

    async def plan(self, **kwargs):
        self.replan_calls.append(kwargs)
        if self.continuation is None:
            raise RuntimeError("replan declined for this test")
        return self.continuation, None
