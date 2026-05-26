"""Executor integration tests for step 28:
  - `tool_creator` step kind invokes ToolCreatorAgent + persists outcome
  - `_run_functional` falls through to user-tools when the registry has no match
  - builtins still win when both a builtin AND a user-tool share a name."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from wolfpaw.agents.executor import ExecutorAgent
from wolfpaw.memory.tools import UserTool
from wolfpaw.metering.model_client import ModelClient
from wolfpaw.schemas import Plan, Step, StepStatus
from wolfpaw.toolbox.registry import Registry, Tool, ToolContext


@asynccontextmanager
async def _fake_acquire():
    yield None


class FakeAnthropic:
    def __init__(self, replies=()):
        self._replies = list(replies)
        self.messages = self
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(dict(kwargs))
        text = self._replies.pop(0) if self._replies else "(synth)"
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=10, output_tokens=5,
                cache_read_input_tokens=0, cache_creation_input_tokens=0,
            ),
        )


@pytest.fixture
def exec_env(monkeypatch):
    async def fake_update(_conn, **_kw):
        return None

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

    monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.agents.executor.acquire", _fake_acquire)
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.acquire", _fake_acquire,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.executor.procedural.update_outcome", fake_update,
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


def _plan(steps: list[Step]) -> Plan:
    return Plan(
        query="q", summary="s", steps=steps,
        id=uuid4(), model_used="claude-sonnet-4-6",
    )


def _ctx() -> ToolContext:
    return ToolContext(user_id=uuid4(), task_id=uuid4())


# --- tool_creator step kind -----------------------------------------------


async def test_executor_dispatches_tool_creator_step_through_agent(
    exec_env, monkeypatch,
):
    """A `tool_creator` step lands the ToolCreatorAgent's outcome into
    the step's output. We mock the agent to verify the wiring."""
    from wolfpaw.agents.tool_creator import ToolCreationOutcome

    captured: list[dict] = []

    class _FakeAgent:
        async def create_tool(self, *, ctx, intent, required_inputs, plan_id):
            captured.append({
                "intent": intent,
                "required_inputs": required_inputs,
                "plan_id": plan_id,
            })
            return ToolCreationOutcome(
                status="approved",
                tool_id=uuid4(),
                name="emit_csv",
                message="user approved",
            )

    monkeypatch.setattr(
        "wolfpaw.agents.tool_creator.get_tool_creator_agent",
        lambda: _FakeAgent(),
    )

    plan = _plan([
        Step(
            id="propose_csv_tool", kind="tool_creator",
            description="Create a CSV emit tool.",
            inputs={
                "intent": "Emit a CSV from a list of dicts.",
                "required_inputs": ["rows", "filename"],
            },
        ),
    ])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=FakeAnthropic()),
        registry=Registry(),
    )
    execution = await executor.execute(ctx=_ctx(), plan=plan)
    assert execution.success
    assert len(captured) == 1
    assert captured[0]["intent"] == "Emit a CSV from a list of dicts."
    assert captured[0]["required_inputs"] == ["rows", "filename"]
    assert captured[0]["plan_id"] == plan.id
    # Step output carries the outcome dict.
    res = execution.results[0]
    assert res.status == StepStatus.COMPLETED
    assert res.output["status"] == "approved"
    assert res.output["name"] == "emit_csv"


async def test_executor_fails_tool_creator_step_with_no_intent(exec_env):
    """A `tool_creator` step missing inputs.intent surfaces as a step
    failure rather than crashing inside the agent."""
    plan = _plan([
        Step(id="bad", kind="tool_creator", description="x", inputs={}),
    ])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=FakeAnthropic()),
        registry=Registry(),
    )
    execution = await executor.execute(ctx=_ctx(), plan=plan)
    assert not execution.success
    assert "inputs.intent" in execution.results[0].error


async def test_executor_fails_tool_creator_step_outside_task(exec_env):
    """tool_creator requires ctx.task_id because ask_user does. With
    no task context, the step fails before invoking the agent."""
    plan = _plan([
        Step(id="x", kind="tool_creator", description="x",
             inputs={"intent": "do X"}),
    ])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=FakeAnthropic()),
        registry=Registry(),
    )
    no_task_ctx = ToolContext(user_id=uuid4(), task_id=None)
    execution = await executor.execute(ctx=no_task_ctx, plan=plan)
    assert not execution.success
    assert "parent task" in execution.results[0].error


# --- _run_functional fallthrough to user-tools ----------------------------


class _StaticTool(Tool):
    """Records run() calls + returns a fixed dict. For asserting which
    Tool actually got invoked when builtin + user-tool names collide."""

    def __init__(self, name: str, output: dict[str, Any]) -> None:
        self.name = name
        self.description = f"{name} stub"
        self.input_schema = {"type": "object", "properties": {}}
        self._output = output
        self.calls: list[dict] = []

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        self.calls.append(inputs)
        return self._output


async def test_executor_dispatches_user_tool_on_registry_miss(
    exec_env, monkeypatch,
):
    """When the builtin Registry has no match for a step.tool name,
    the executor falls through to memory.tools.find_active_by_name
    and dispatches a DynamicUserTool."""
    user_tool_row = UserTool(
        id=uuid4(), user_id=uuid4(),
        name="emit_csv",
        description="emit a csv",
        signature={
            "type": "object",
            "properties": {"rows": {"type": "array"}},
            "required": ["rows"],
        },
        implementation="result = {'wrote': len(inputs['rows'])}",
        status="approved",
    )

    async def fake_find(_conn, *, user_id, name):
        if name == "emit_csv":
            return user_tool_row
        return None

    # Stub the sandbox manager so DynamicUserTool can run.
    class _FakeSandbox:
        def __init__(self):
            self.files: dict[str, bytes] = {}

        async def write_file(self, name: str, data: bytes) -> None:
            self.files[name] = data

        async def read_file(self, name: str) -> bytes:
            return self.files[name]

        async def run_python(self, script: str):
            files = self.files

            class _FakeFile:
                def __init__(self, n, m):
                    self._n, self._m, self._buf = n, m, b""

                def __enter__(self):
                    if "r" in self._m:
                        self._buf = files[self._n]
                    return self

                def __exit__(self, *exc):
                    if "w" in self._m:
                        files[self._n] = self._buf
                    return False

                def read(self):
                    return self._buf.decode("utf-8")

                def write(self, data):
                    self._buf += data.encode("utf-8")

            def fake_open(name, mode="r", *args, **kwargs):
                return _FakeFile(name, mode)

            ns = {"__builtins__": __builtins__, "open": fake_open}
            try:
                exec(script, ns)
            except Exception:  # noqa: BLE001
                return SimpleNamespace(
                    exit_code=1, stdout="", stderr="exc",
                    timed_out=False, elapsed_seconds=0.0,
                )
            return SimpleNamespace(
                exit_code=0, stdout="", stderr="",
                timed_out=False, elapsed_seconds=0.0,
            )

    sandbox = _FakeSandbox()

    class _Manager:
        async def get(self, user_id, task_id):
            return sandbox

    monkeypatch.setattr(
        "wolfpaw.memory.tools.find_active_by_name", fake_find,
    )
    monkeypatch.setattr(
        "wolfpaw.toolbox.tools._dynamic_user_tool.get_manager",
        lambda: _Manager(),
    )

    plan = _plan([
        Step(
            id="emit", kind="functional", tool="emit_csv",
            description="emit",
            inputs={"rows": [{"a": 1}, {"a": 2}, {"a": 3}]},
        ),
    ])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=FakeAnthropic(replies=["synth"])),
        registry=Registry(),  # empty — forces fallthrough
    )
    execution = await executor.execute(ctx=_ctx(), plan=plan)
    assert execution.success
    res = execution.results[0]
    assert res.status == StepStatus.COMPLETED
    assert res.output == {"wrote": 3}


async def test_executor_prefers_builtin_when_user_tool_has_same_name(
    exec_env, monkeypatch,
):
    """Builtins always win the name collision. If a user-tool happens
    to share a name with a builtin, the builtin's run() is what
    dispatches — the DB lookup is never invoked."""
    builtin = _StaticTool("conflict", {"src": "builtin"})

    async def fake_find(_conn, *, user_id, name):
        # Should NOT be reached because the builtin matched first.
        raise AssertionError("user-tool lookup must not fire on builtin hit")

    monkeypatch.setattr(
        "wolfpaw.memory.tools.find_active_by_name", fake_find,
    )

    registry = Registry()
    registry.register(builtin)

    plan = _plan([
        Step(id="run", kind="functional", tool="conflict", description="x"),
    ])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=FakeAnthropic(replies=["synth"])),
        registry=registry,
    )
    execution = await executor.execute(ctx=_ctx(), plan=plan)
    assert execution.success
    assert execution.results[0].output == {"src": "builtin"}
    assert len(builtin.calls) == 1
