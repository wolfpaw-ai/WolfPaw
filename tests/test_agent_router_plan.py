"""Router tests for the plan route — composes Planner + Executor.

`test_agent_router.py` covers quick / task verdicts in isolation; this
file owns the multi-agent plan path (Triage → Planner → Executor →
final answer + persistence)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

import pytest

from wolfpaw.agents.router import Router
from wolfpaw.agents.triage import TriageVerdict
from wolfpaw.schemas import ExecutionPlan, Plan, Step, StepResult, StepStatus
from wolfpaw.toolbox.registry import ToolContext


@asynccontextmanager
async def _fake_acquire():
    yield None


@pytest.fixture(autouse=True)
def _stub_persistence(monkeypatch):
    """Stub out conv.append so the plan route's user + final-text writes
    don't need Postgres."""
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


class FakePlanner:
    def __init__(self, plan: Plan, plan_ctx=None):
        self._plan = plan
        self._plan_ctx = plan_ctx
        self.calls: list[dict] = []

    async def plan(self, *, ctx, thread_id, content, complexity_hint="moderate"):
        self.calls.append({
            "content": content, "complexity_hint": complexity_hint,
            "thread_id": thread_id,
        })
        return self._plan, self._plan_ctx or _empty_plan_ctx()


class FakeExecutor:
    def __init__(self, execution: ExecutionPlan):
        self._execution = execution
        self.calls: list[dict] = []

    async def execute(self, *, ctx, plan, emit=None):
        self.calls.append({"plan_id": plan.id, "emit_is_none": emit is None})
        if emit is not None:
            await emit("step.start", "s1")
            await emit("step.end", "s1 done")
        return self._execution


def _empty_plan_ctx():
    from wolfpaw.agents.planner import PlanContext
    return PlanContext(past_plans=[], relevant_skills=[])


def _plan(*, summary="Do it.", n_steps=2, is_task=False, skill=None) -> Plan:
    return Plan(
        query="q", summary=summary,
        steps=[
            Step(id=f"s{i}", kind="reasoning", description=f"step {i}")
            for i in range(n_steps)
        ],
        is_task=is_task, model_used="claude-sonnet-4-6",
        applied_skill_name=skill, id=uuid4(),
    )


def _execution(plan: Plan, *, final_answer: str, success: bool = True) -> ExecutionPlan:
    return ExecutionPlan(
        plan=plan,
        results=[
            StepResult(
                step_id=s.id, kind=s.kind,
                status=StepStatus.COMPLETED if success else StepStatus.SKIPPED,
                output="ok",
            )
            for s in plan.steps
        ],
        final_answer=final_answer, success=success,
    )


def _ctx():
    return ToolContext(user_id=uuid4())


# --- tests ----------------------------------------------------------------


async def test_plan_route_runs_planner_then_executor():
    plan = _plan()
    planner = FakePlanner(plan=plan)
    executor = FakeExecutor(_execution(plan, final_answer="final synth"))
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="moderate", reasoning="r"),
        ),
        planner=planner, executor=executor,
    )
    text = await router.handle(ctx=_ctx(), thread_id=uuid4(), content="x")
    assert text == "final synth"
    assert len(planner.calls) == 1
    assert len(executor.calls) == 1
    assert executor.calls[0]["plan_id"] == plan.id


async def test_plan_route_passes_complexity_hint_through():
    plan = _plan()
    planner = FakePlanner(plan=plan)
    executor = FakeExecutor(_execution(plan, final_answer="ok"))
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="ambitious", reasoning="r"),
        ),
        planner=planner, executor=executor,
    )
    await router.handle(ctx=_ctx(), thread_id=uuid4(), content="big job")
    assert planner.calls[0]["complexity_hint"] == "ambitious"


async def test_plan_route_persists_user_and_final_text(_stub_persistence):
    appended = _stub_persistence
    plan = _plan()
    planner = FakePlanner(plan=plan)
    executor = FakeExecutor(_execution(plan, final_answer="the answer"))
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="moderate", reasoning="r"),
        ),
        planner=planner, executor=executor,
    )
    await router.handle(ctx=_ctx(), thread_id=uuid4(), content="ask")
    roles = [a["role"] for a in appended]
    contents = [a["content"] for a in appended]
    assert roles == ["user", "assistant"]
    assert contents == ["ask", "the answer"]


async def test_plan_route_emits_plan_event_with_summary():
    emitted: list[tuple[str, str]] = []

    async def emit(event, data):
        emitted.append((event, data))

    plan = _plan(n_steps=3, skill="vendor_comparison_spreadsheet")
    planner = FakePlanner(plan=plan)
    executor = FakeExecutor(_execution(plan, final_answer="x"))
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="moderate", reasoning="r"),
        ),
        planner=planner, executor=executor,
    )
    await router.handle(ctx=_ctx(), thread_id=uuid4(), content="x", emit=emit)
    plan_events = [e for e in emitted if e[0] == "plan"]
    assert len(plan_events) == 1
    assert "3 steps" in plan_events[0][1]
    assert "vendor_comparison_spreadsheet" in plan_events[0][1]


async def test_plan_route_propagates_emit_into_executor():
    emitted: list[tuple[str, str]] = []

    async def emit(event, data):
        emitted.append((event, data))

    plan = _plan()
    planner = FakePlanner(plan=plan)
    executor = FakeExecutor(_execution(plan, final_answer="x"))
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="moderate", reasoning="r"),
        ),
        planner=planner, executor=executor,
    )
    await router.handle(ctx=_ctx(), thread_id=uuid4(), content="x", emit=emit)
    kinds = [e for e, _ in emitted]
    assert "step.start" in kinds
    assert "step.end" in kinds


async def test_plan_route_returns_executor_failure_as_final_answer():
    plan = _plan()
    planner = FakePlanner(plan=plan)
    executor = FakeExecutor(
        _execution(plan, final_answer="I ran into a problem.", success=False),
    )
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="moderate", reasoning="r"),
        ),
        planner=planner, executor=executor,
    )
    text = await router.handle(ctx=_ctx(), thread_id=uuid4(), content="x")
    assert text == "I ran into a problem."
