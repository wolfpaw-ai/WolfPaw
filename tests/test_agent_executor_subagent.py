"""Executor's subagent step dispatch — happy path, depth cap, child
failure, parallel-group concurrency. TaskService is faked so the
executor's branch logic is tested in isolation."""

from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Iterable
from uuid import UUID, uuid4

import pytest

from wolfpaw.agents.executor import ExecutorAgent, MAX_SUBAGENT_DEPTH
from wolfpaw.memory.tasks import Task
from wolfpaw.metering.model_client import ModelClient
from wolfpaw.schemas import (
    ExecutionPlan, Plan, Step, StepResult, StepStatus,
)
from wolfpaw.tasks.service import TaskOutcome
from wolfpaw.toolbox.registry import Registry, ToolContext


@asynccontextmanager
async def _fake_acquire():
    yield None


# --- fakes ---------------------------------------------------------------


class FakeAnthropic:
    """Minimal stand-in for reasoning/synthesis calls. Subagent steps
    don't go through this — they're handed off to FakeTaskService."""

    def __init__(self, replies: Iterable[str]) -> None:
        self._replies = list(replies)
        self.messages = self
        self.calls: list[dict[str, Any]] = []

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


@dataclass
class _ChildResult:
    answer: str = "child answer"
    status: str = "completed"
    delay: float = 0.0
    score: int | None = 88


class FakeTaskService:
    def __init__(self, child_results: list[_ChildResult] | None = None):
        self._results = list(child_results or [_ChildResult()])
        self.calls: list[dict[str, Any]] = []

    async def create_and_run(self, *, user_id, thread_id, content, title,
                             description=None, channel_for_completion=None,
                             complexity_hint="moderate", emit=None,
                             parent_task_id=None, budget_cents=None):
        self.calls.append({
            "user_id": user_id, "content": content, "title": title,
            "parent_task_id": parent_task_id, "budget_cents": budget_cents,
            "complexity_hint": complexity_hint, "emit": emit,
        })
        if not self._results:
            raise AssertionError("FakeTaskService out of canned results")
        child = self._results.pop(0)
        if child.delay:
            await asyncio.sleep(child.delay)
        verdict = (
            SimpleNamespace(score=child.score)
            if child.score is not None else None
        )
        fake_task = Task(
            id=uuid4(), user_id=user_id, parent_task_id=parent_task_id,
            title=title, description=description, status=child.status,
            current_plan_id=None, budget_cents=budget_cents, spent_cents=0,
            blocking_reason=None,
            channel_for_completion=channel_for_completion,
            schedule_pattern=None, created_at=datetime.now(timezone.utc),
            started_at=None, completed_at=None, last_active_at=None,
        )
        return TaskOutcome(
            task=fake_task, plan=None, execution=None,
            verdict=verdict, final_answer=child.answer,
        )


@dataclass
class _DepthLookup:
    by_task_id: dict[UUID, int] = field(default_factory=dict)
    default: int = 0


@pytest.fixture
def exec_env(monkeypatch):
    fake_service = FakeTaskService()
    monkeypatch.setattr(
        "wolfpaw.tasks.service.get_task_service", lambda: fake_service,
    )

    depth_lookup = _DepthLookup()

    async def fake_get_depth(_conn, *, task_id):
        return depth_lookup.by_task_id.get(task_id, depth_lookup.default)

    async def fake_get_root(_conn, *, task_id):
        # Tests don't set up multi-level parent chains; treat the
        # task_id itself as the root (which is true for the synthetic
        # plans these tests use).
        return task_id

    async def fake_update_outcome(_conn, *, plan_id, final_answer=None,
                                  success=None, score=None, error=None):
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
        "wolfpaw.agents.executor.tasks_dao.get_depth", fake_get_depth,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.executor.tasks_dao.get_root", fake_get_root,
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
        "wolfpaw.agents.executor.get_sandbox_manager",
        lambda: _SandboxStub(),
    )
    yield {"service": fake_service, "depth": depth_lookup}


def _plan(steps: list[Step]) -> Plan:
    return Plan(
        query="q", summary="s", steps=steps,
        id=uuid4(), model_used="claude-sonnet-4-6",
    )


def _ctx(task_id: UUID | None = None) -> ToolContext:
    return ToolContext(user_id=uuid4(), task_id=task_id or uuid4())


# --- tests ---------------------------------------------------------------


async def test_subagent_step_spawns_child_and_captures_answer(exec_env):
    """Happy path: one subagent step → TaskService.create_and_run called
    with the right inputs → child's answer becomes the step output."""
    fake = FakeAnthropic(replies=[])  # no model call expected for a
                                       # single-step subagent plan
                                       # (last completed = subagent dict,
                                       # not reasoning → synthesis call)
    fake.calls = []
    fake_replies = ["synthesized final answer"]
    fake._replies = fake_replies

    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake),
        registry=Registry(),
    )
    plan = _plan([
        Step(
            id="research",
            kind="subagent",
            description="Research vendor A",
            inputs={
                "query": "look up vendor A's pricing",
                "title": "Vendor A research",
                "budget_cents": 5000,
            },
        ),
    ])
    execution = await executor.execute(ctx=_ctx(), plan=plan)
    assert execution.success
    # Service called once with the spawn args.
    assert len(exec_env["service"].calls) == 1
    call = exec_env["service"].calls[0]
    assert call["content"] == "look up vendor A's pricing"
    assert call["title"] == "Vendor A research"
    assert call["budget_cents"] == 5000
    assert call["parent_task_id"] is not None  # the parent ctx.task_id
    # Step result records the child task id + answer.
    res = next(r for r in execution.results if r.step_id == "research")
    assert res.status == StepStatus.COMPLETED
    assert res.output["answer"] == "child answer"
    assert UUID(res.output["subagent_task_id"])  # valid uuid
    # Synthesis call fired because the last completed step wasn't reasoning.
    assert len(fake.calls) == 1
    assert execution.final_answer == "synthesized final answer"


async def test_subagent_step_propagates_complexity_hint(exec_env):
    fake = FakeAnthropic(replies=["synth"])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake), registry=Registry(),
    )
    plan = _plan([
        Step(id="s", kind="subagent", description="x",
             inputs={"query": "do it", "complexity_hint": "ambitious"}),
    ])
    await executor.execute(ctx=_ctx(), plan=plan)
    assert exec_env["service"].calls[0]["complexity_hint"] == "ambitious"


async def test_subagent_step_emit_not_propagated(exec_env):
    """Subagent execution mustn't dump events into the parent's stream —
    parallel subagents would interleave confusingly. The parent's
    step.start/step.end already mark the subagent boundary."""
    fake = FakeAnthropic(replies=["synth"])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake), registry=Registry(),
    )
    plan = _plan([
        Step(id="s", kind="subagent", description="x",
             inputs={"query": "do it"}),
    ])

    async def emit(event, data):
        pass

    await executor.execute(ctx=_ctx(), plan=plan, emit=emit)
    # The fake task service captured the emit kwarg.
    assert exec_env["service"].calls[0]["emit"] is None


async def test_subagent_step_requires_ctx_task_id(exec_env):
    fake = FakeAnthropic(replies=[])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake), registry=Registry(),
    )
    plan = _plan([
        Step(id="s", kind="subagent", description="x",
             inputs={"query": "do it"}),
    ])
    # ctx without task_id — the subagent step should fail.
    no_task_ctx = ToolContext(user_id=uuid4(), task_id=None)
    execution = await executor.execute(ctx=no_task_ctx, plan=plan)
    assert not execution.success
    assert "subagent steps require a parent task" in execution.results[0].error


async def test_subagent_step_requires_query(exec_env):
    fake = FakeAnthropic(replies=[])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake), registry=Registry(),
    )
    plan = _plan([
        Step(id="s", kind="subagent", description="x", inputs={}),
    ])
    execution = await executor.execute(ctx=_ctx(), plan=plan)
    assert not execution.success
    assert "inputs.query" in execution.results[0].error


async def test_subagent_step_rejected_at_max_depth(exec_env):
    """If the parent is already MAX_SUBAGENT_DEPTH deep, spawning a
    subagent step would push the child over the cap → rejected."""
    fake = FakeAnthropic(replies=[])
    parent_task = uuid4()
    exec_env["depth"].by_task_id[parent_task] = MAX_SUBAGENT_DEPTH
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake), registry=Registry(),
    )
    plan = _plan([
        Step(id="s", kind="subagent", description="x",
             inputs={"query": "do it"}),
    ])
    execution = await executor.execute(
        ctx=_ctx(task_id=parent_task), plan=plan,
    )
    assert not execution.success
    assert "depth cap" in execution.results[0].error
    # No spawn happened.
    assert exec_env["service"].calls == []


async def test_subagent_step_child_failure_propagates(monkeypatch, exec_env):
    """If the child task ends non-completed, the parent step fails with
    the child's final_answer in the error message."""
    fake_service = FakeTaskService([
        _ChildResult(answer="child blew up", status="failed", score=None),
    ])
    monkeypatch.setattr(
        "wolfpaw.tasks.service.get_task_service", lambda: fake_service,
    )
    fake = FakeAnthropic(replies=[])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake), registry=Registry(),
    )
    plan = _plan([
        Step(id="s", kind="subagent", description="x",
             inputs={"query": "do it"}),
    ])
    execution = await executor.execute(ctx=_ctx(), plan=plan)
    assert not execution.success
    err = execution.results[0].error
    assert "failed" in err
    assert "child blew up" in err


async def test_parallel_subagent_steps_dispatched_concurrently(
    monkeypatch, exec_env,
):
    """Two subagent steps in the same parallel_group → both spawn at the
    same time via asyncio.gather. Wall time should be ~1 delay, not 2x."""
    fake_service = FakeTaskService([
        _ChildResult(answer="A", delay=0.10),
        _ChildResult(answer="B", delay=0.10),
    ])
    monkeypatch.setattr(
        "wolfpaw.tasks.service.get_task_service", lambda: fake_service,
    )
    fake = FakeAnthropic(replies=["synth"])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake), registry=Registry(),
    )
    plan = _plan([
        Step(id="a", kind="subagent", description="A",
             inputs={"query": "qa"}, parallel_group=1),
        Step(id="b", kind="subagent", description="B",
             inputs={"query": "qb"}, parallel_group=1),
    ])
    import time
    start = time.monotonic()
    execution = await executor.execute(ctx=_ctx(), plan=plan)
    elapsed = time.monotonic() - start
    assert execution.success
    assert elapsed < 0.18, f"subagents not concurrent (took {elapsed:.2f}s)"


async def test_subagent_concurrency_capped_per_root(monkeypatch, exec_env):
    """7 sibling subagents share a per-root cap of 5 — at most 5 run
    concurrently, the remaining 2 wait until a slot frees up."""
    from wolfpaw.agents import executor as exec_mod

    in_flight = {"current": 0, "max_seen": 0}

    async def gated_create_and_run(**kwargs):
        in_flight["current"] += 1
        in_flight["max_seen"] = max(in_flight["max_seen"], in_flight["current"])
        try:
            # Long enough that all "first batch" siblings overlap before
            # any of them release the slot.
            await asyncio.sleep(0.05)
        finally:
            in_flight["current"] -= 1
        # Build a successful outcome the same way FakeTaskService would.
        fake_task = Task(
            id=uuid4(), user_id=kwargs["user_id"],
            parent_task_id=kwargs.get("parent_task_id"),
            title=kwargs["title"], description=kwargs.get("description"),
            status="completed", current_plan_id=None,
            budget_cents=kwargs.get("budget_cents"), spent_cents=0,
            blocking_reason=None,
            channel_for_completion=kwargs.get("channel_for_completion"),
            schedule_pattern=None,
            created_at=datetime.now(timezone.utc),
            started_at=None, completed_at=None, last_active_at=None,
        )
        return TaskOutcome(
            task=fake_task, plan=None, execution=None,
            verdict=SimpleNamespace(score=80),
            final_answer="ok",
        )

    fake_service = SimpleNamespace(create_and_run=gated_create_and_run)
    monkeypatch.setattr(
        "wolfpaw.tasks.service.get_task_service", lambda: fake_service,
    )

    fake = FakeAnthropic(replies=["synth"])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake), registry=Registry(),
    )
    plan = _plan([
        Step(id=f"s{i}", kind="subagent", description=f"step {i}",
             inputs={"query": f"q{i}"}, parallel_group=1)
        for i in range(7)
    ])
    execution = await executor.execute(ctx=_ctx(), plan=plan)
    assert execution.success
    assert in_flight["max_seen"] == exec_mod.MAX_CONCURRENT_SUBAGENTS_PER_ROOT


# --- step 27: structured failure policies --------------------------------


async def test_subagent_on_failure_default_fails_step(monkeypatch, exec_env):
    """No `on_failure` set → step fails when the child task fails
    (same as pre-step-27 behaviour)."""
    fake_service = FakeTaskService([
        _ChildResult(answer="child blew up", status="failed", score=None),
    ])
    monkeypatch.setattr(
        "wolfpaw.tasks.service.get_task_service", lambda: fake_service,
    )
    fake = FakeAnthropic(replies=[])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake), registry=Registry(),
    )
    plan = _plan([
        Step(id="s", kind="subagent", description="x",
             inputs={"query": "do it"}),
    ])
    execution = await executor.execute(ctx=_ctx(), plan=plan)
    assert not execution.success
    assert "failed" in execution.results[0].error
    # Exactly one spawn — no retry.
    assert len(fake_service.calls) == 1


async def test_subagent_on_failure_drop_returns_stub_and_completes_step(
    monkeypatch, exec_env,
):
    """`on_failure="drop"` → step completes with a stub output marked
    dropped=True, the rest of the plan keeps going."""
    fake_service = FakeTaskService([
        _ChildResult(answer="branch died", status="failed", score=None),
    ])
    monkeypatch.setattr(
        "wolfpaw.tasks.service.get_task_service", lambda: fake_service,
    )
    fake = FakeAnthropic(replies=["synthesized despite drop"])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake), registry=Registry(),
    )
    plan = _plan([
        Step(id="s", kind="subagent", description="x",
             inputs={"query": "do it", "on_failure": "drop"}),
    ])
    execution = await executor.execute(ctx=_ctx(), plan=plan)
    assert execution.success
    res = execution.results[0]
    assert res.status == StepStatus.COMPLETED
    assert res.output["dropped"] is True
    assert "branch died" in res.output["error"]
    assert res.output["answer"] is None
    # Only one spawn — drop doesn't retry.
    assert len(fake_service.calls) == 1


async def test_subagent_on_failure_drop_keeps_sibling_subagents_succeeding(
    monkeypatch, exec_env,
):
    """Parallel subagents with on_failure=drop: one fails (dropped),
    the other succeeds. The parent step roster reflects both."""
    fake_service = FakeTaskService([
        _ChildResult(answer="A ok", status="completed"),
        _ChildResult(answer="B failed", status="failed", score=None),
    ])
    monkeypatch.setattr(
        "wolfpaw.tasks.service.get_task_service", lambda: fake_service,
    )
    fake = FakeAnthropic(replies=["combined output"])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake), registry=Registry(),
    )
    plan = _plan([
        Step(id="a", kind="subagent", description="A",
             inputs={"query": "qa", "on_failure": "drop"},
             parallel_group=1),
        Step(id="b", kind="subagent", description="B",
             inputs={"query": "qb", "on_failure": "drop"},
             parallel_group=1),
    ])
    execution = await executor.execute(ctx=_ctx(), plan=plan)
    assert execution.success
    a = next(r for r in execution.results if r.step_id == "a")
    b = next(r for r in execution.results if r.step_id == "b")
    assert a.status == StepStatus.COMPLETED
    assert a.output.get("dropped") is not True
    assert b.status == StepStatus.COMPLETED
    assert b.output["dropped"] is True


async def test_subagent_on_failure_retry_respawns_with_augmented_query(
    monkeypatch, exec_env,
):
    """First attempt fails → retry fires with the failure context
    appended to the query. The second attempt succeeds."""
    fake_service = FakeTaskService([
        _ChildResult(answer="first try crashed", status="failed", score=None),
        _ChildResult(answer="retry worked", status="completed"),
    ])
    monkeypatch.setattr(
        "wolfpaw.tasks.service.get_task_service", lambda: fake_service,
    )
    fake = FakeAnthropic(replies=["synth"])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake), registry=Registry(),
    )
    plan = _plan([
        Step(id="s", kind="subagent", description="x",
             inputs={"query": "look up vendor A",
                     "on_failure": "retry"}),
    ])
    execution = await executor.execute(ctx=_ctx(), plan=plan)
    assert execution.success
    res = execution.results[0]
    assert res.output["answer"] == "retry worked"
    # Two spawns: first the original query, then the augmented one.
    assert len(fake_service.calls) == 2
    assert fake_service.calls[0]["content"] == "look up vendor A"
    retry_content = fake_service.calls[1]["content"]
    assert retry_content.startswith("look up vendor A")
    assert "Previous attempt failed" in retry_content
    assert "first try crashed" in retry_content


async def test_subagent_on_failure_retry_propagates_when_retry_also_fails(
    monkeypatch, exec_env,
):
    """Retry exhausts after one extra attempt — second failure falls
    through to the default "fail" semantics and the step fails."""
    fake_service = FakeTaskService([
        _ChildResult(answer="first crash", status="failed", score=None),
        _ChildResult(answer="retry crash", status="failed", score=None),
    ])
    monkeypatch.setattr(
        "wolfpaw.tasks.service.get_task_service", lambda: fake_service,
    )
    fake = FakeAnthropic(replies=[])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake), registry=Registry(),
    )
    plan = _plan([
        Step(id="s", kind="subagent", description="x",
             inputs={"query": "do it", "on_failure": "retry"}),
    ])
    execution = await executor.execute(ctx=_ctx(), plan=plan)
    assert not execution.success
    # Both spawns happened — exactly one retry, then propagate.
    assert len(fake_service.calls) == 2
    err = execution.results[0].error
    assert "retry crash" in err


async def test_subagent_on_failure_normalizes_unknown_policy_to_fail(
    monkeypatch, exec_env,
):
    """A typo or hallucinated policy value falls back to "fail" rather
    than silently dropping the failure."""
    fake_service = FakeTaskService([
        _ChildResult(answer="x", status="failed", score=None),
    ])
    monkeypatch.setattr(
        "wolfpaw.tasks.service.get_task_service", lambda: fake_service,
    )
    fake = FakeAnthropic(replies=[])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake), registry=Registry(),
    )
    plan = _plan([
        Step(id="s", kind="subagent", description="x",
             inputs={"query": "do it", "on_failure": "yolo"}),
    ])
    execution = await executor.execute(ctx=_ctx(), plan=plan)
    assert not execution.success
    # No retry — "yolo" normalized to "fail".
    assert len(fake_service.calls) == 1


async def test_subagent_on_failure_drop_doesnt_swallow_input_validation(
    monkeypatch, exec_env,
):
    """The failure policy applies to subagent-task failures, NOT to
    input validation errors. A missing query still fails the step
    regardless of `on_failure`."""
    fake = FakeAnthropic(replies=[])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake), registry=Registry(),
    )
    plan = _plan([
        Step(id="s", kind="subagent", description="x",
             # `query` missing — should still surface as a real failure
             # even though on_failure="drop".
             inputs={"on_failure": "drop"}),
    ])
    execution = await executor.execute(ctx=_ctx(), plan=plan)
    assert not execution.success
    assert "inputs.query" in execution.results[0].error


async def test_subagent_with_trailing_reasoning_step_skips_synthesis(
    monkeypatch, exec_env,
):
    """When the planner ends the plan with a reasoning step that
    synthesizes the subagent outputs, the executor uses that reasoning
    step's text directly — no extra synthesis call."""
    fake = FakeAnthropic(replies=["final synthesis from reasoning step"])
    executor = ExecutorAgent(
        model_client=ModelClient(anthropic=fake), registry=Registry(),
    )
    plan = _plan([
        Step(id="s1", kind="subagent", description="A",
             inputs={"query": "qa"}),
        Step(id="s2", kind="reasoning", description="synthesize"),
    ])
    execution = await executor.execute(ctx=_ctx(), plan=plan)
    assert execution.success
    assert execution.final_answer == "final synthesis from reasoning step"
    # Only ONE model call — the reasoning step. No extra synth.
    assert len(fake.calls) == 1
