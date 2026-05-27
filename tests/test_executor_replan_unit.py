"""Mid-plan replan on failure (feature 3).

When a step fails after step-level retry exhausted, the Executor calls
the Planner with a ReplanContext. The Planner returns a continuation
plan whose steps are spliced into the remaining plan. Bounded to
``_MAX_REPLANS`` per execution.
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
from wolfpaw.schemas import Plan, ReplanContext, Step, StepStatus
from wolfpaw.toolbox.registry import Registry, ToolContext, ToolError


@asynccontextmanager
async def _fake_acquire():
    yield None


class FakeAnthropic:
    """Same shape as the recovery test stub — replies are popped one by
    one, each either a string (text/synthesis) or a tool_use payload."""

    def __init__(self, replies: Iterable[Any]) -> None:
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    async def create(self, **kwargs):
        snapshot = dict(kwargs)
        if "messages" in snapshot:
            snapshot["messages"] = copy.deepcopy(snapshot["messages"])
        self.calls.append(snapshot)
        reply = self._replies.pop(0) if self._replies else "(no reply)"
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


class _ReplanPlanner:
    """Test double for PlannerAgent.plan() — records the call and
    returns either a continuation plan or raises (simulating 'replan
    declined')."""

    def __init__(self, *, continuation: Plan | None):
        self.continuation = continuation
        self.calls: list[dict] = []

    async def plan(self, **kwargs):
        self.calls.append(kwargs)
        if self.continuation is None:
            raise RuntimeError("replan declined for this test")
        return self.continuation, None


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


def _plan(steps: list[Step], *, query: str = "q", plan_id: UUID | None = None) -> Plan:
    return Plan(
        query=query, summary="run", steps=steps,
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


async def test_failure_triggers_replan_and_continuation_runs(exec_env):
    """A step fails twice (exhausts step-level retry). Executor asks the
    Planner for a continuation; the continuation's single step succeeds
    and execution ends successful."""
    fail_tool = _SeqTool(
        "sql_insert",
        outcomes=[
            ToolError("column 'title' missing — try describe_table first"),
            ToolError("still 'title' missing"),
        ],
    )
    recovered_tool = _SeqTool(
        "describe_table",
        outcomes=[{"columns": [{"name": "id"}, {"name": "vendor"}]}],
    )
    continuation = _plan(
        [Step(id="describe", kind="functional", description="introspect",
              tool="describe_table", inputs={"table_name": "recipes"})],
    )
    planner = _ReplanPlanner(continuation=continuation)
    fake = FakeAnthropic(replies=[
        # Recover model call #1 — proposes (doomed) corrected inputs.
        {"tool": "propose_inputs", "input": {
            "reasoning": "try again",
            "corrected_inputs": {"x": 1},
        }},
        # Final synthesis after the replanned continuation step succeeds.
        "Looked up the schema.",
    ])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake),
        registry=_registry(fail_tool, recovered_tool),
        planner=planner,
    )
    plan = _plan([
        Step(
            id="ins", kind="functional", description="insert",
            tool="sql_insert", inputs={"table_name": "recipes"},
        ),
    ], query="store the meatloaf recipe", plan_id=uuid4())

    execution = await executor.execute(ctx=_ctx(), plan=plan)

    assert execution.success is True
    # Planner.plan() was called once (replan).
    assert len(planner.calls) == 1
    replan_ctx = planner.calls[0]["replan_from"]
    assert isinstance(replan_ctx, ReplanContext)
    assert replan_ctx.failed_step.id == "ins"
    assert "title" in replan_ctx.failed_step_error
    # Continuation tool ran exactly once.
    assert len(recovered_tool.calls) == 1
    # Last result is the continuation step, succeeded.
    assert execution.results[-1].step_id == "describe"
    assert execution.results[-1].status == StepStatus.COMPLETED


async def test_replan_declined_falls_through_to_skip(exec_env):
    """When the Planner can't / won't replan, the Executor falls back
    to the pre-existing 'mark remaining steps SKIPPED and stop' path."""
    fail_tool = _SeqTool(
        "sql_insert",
        outcomes=[ToolError("boom"), ToolError("boom again")],
    )
    planner = _ReplanPlanner(continuation=None)  # raises on .plan()
    fake = FakeAnthropic(replies=[
        # Recover call also can't fix it.
        {"tool": "propose_inputs", "input": {
            "reasoning": "no idea", "give_up": True,
        }},
    ])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake),
        registry=_registry(fail_tool),
        planner=planner,
    )
    plan = _plan([
        Step(id="ins", kind="functional", description="insert",
             tool="sql_insert", inputs={"x": 1}),
        Step(id="never", kind="functional", description="never runs",
             tool="sql_insert", inputs={"x": 2}),
    ], plan_id=uuid4())

    execution = await executor.execute(ctx=_ctx(), plan=plan)

    assert execution.success is False
    # Replan was attempted and raised (so falls through to skip).
    assert len(planner.calls) == 1
    statuses = [r.status for r in execution.results]
    assert statuses == [StepStatus.FAILED, StepStatus.SKIPPED]


async def test_replan_budget_caps_at_max_replans(exec_env):
    """If the continuation itself fails after replan budget consumed,
    no second replan happens — fall through to skip."""
    fail_tool = _SeqTool(
        "always_fail",
        outcomes=[
            ToolError("first plan fail"),
            ToolError("first plan fail again"),
            ToolError("continuation fail"),
            ToolError("continuation fail again"),
        ],
    )
    continuation = _plan(
        [Step(id="retry", kind="functional", description="retry",
              tool="always_fail", inputs={"y": 1})],
    )
    planner = _ReplanPlanner(continuation=continuation)
    fake = FakeAnthropic(replies=[
        # Recover for original step.
        {"tool": "propose_inputs", "input": {
            "reasoning": "ok", "corrected_inputs": {"x": 2},
        }},
        # Recover for continuation step.
        {"tool": "propose_inputs", "input": {
            "reasoning": "ok", "corrected_inputs": {"y": 2},
        }},
    ])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake),
        registry=_registry(fail_tool),
        planner=planner,
    )
    plan = _plan([
        Step(id="ins", kind="functional", description="insert",
             tool="always_fail", inputs={"x": 1}),
    ], plan_id=uuid4())

    execution = await executor.execute(ctx=_ctx(), plan=plan)

    assert execution.success is False
    # Exactly ONE replan despite both the original AND the continuation
    # failing — _MAX_REPLANS = 1.
    assert len(planner.calls) == 1
