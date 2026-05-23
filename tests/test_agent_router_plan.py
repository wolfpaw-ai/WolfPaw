"""Router tests for the plan route — calls Planner, renders preview,
emits the `plan` SSE event, and falls back gracefully on planner errors.
Complements test_agent_router.py which covers quick/task verdicts."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

import pytest

from wolfpaw.agents.router import Router, _render_plan_preview
from wolfpaw.agents.triage import TriageVerdict
from wolfpaw.schemas import Plan, Step
from wolfpaw.toolbox.registry import ToolContext


@asynccontextmanager
async def _fake_acquire():
    yield None


@pytest.fixture(autouse=True)
def _stub_persistence(monkeypatch):
    """Stub out the conv.append calls the plan-route makes to persist
    user + plan-preview turns."""
    async def fake_append(_conn, **_kw):
        return uuid4()

    monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.agents.router.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.agents.router.conv.append", fake_append)


@dataclass
class FakeTriage:
    verdict: TriageVerdict

    async def classify(self, *, ctx, thread_id, content):
        return self.verdict


class FakePlanner:
    def __init__(self, plan: Plan, plan_ctx=None):
        # Underscore-prefixed so the attribute doesn't shadow the method.
        self._plan = plan
        self._plan_ctx = plan_ctx
        self.calls: list[dict] = []

    async def plan(self, *, ctx, thread_id, content, complexity_hint="moderate"):
        self.calls.append({
            "content": content,
            "complexity_hint": complexity_hint,
            "thread_id": thread_id,
        })
        # Return (Plan, PlanContext) shape.
        return self._plan, self._plan_ctx or _empty_plan_ctx()


def _empty_plan_ctx():
    from wolfpaw.agents.planner import PlanContext
    return PlanContext(past_plans=[], relevant_skills=[])


def _plan(*, summary="Do it.", n_steps=2, is_task=False, skill=None) -> Plan:
    return Plan(
        query="q",
        summary=summary,
        steps=[
            Step(id=f"s{i}", kind="reasoning", description=f"step {i}")
            for i in range(n_steps)
        ],
        is_task=is_task,
        model_used="claude-sonnet-4-6",
        applied_skill_name=skill,
        id=uuid4(),
    )


def _ctx():
    return ToolContext(user_id=uuid4())


async def test_plan_route_calls_planner_with_complexity_hint():
    planner = FakePlanner(plan=_plan())
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="ambitious", reasoning="r"),
        ),
        planner=planner,
    )
    await router.handle(ctx=_ctx(), thread_id=uuid4(), content="big job")
    assert len(planner.calls) == 1
    assert planner.calls[0]["complexity_hint"] == "ambitious"
    assert planner.calls[0]["content"] == "big job"


async def test_plan_route_returns_preview_with_steps_and_executor_note():
    planner = FakePlanner(
        plan=_plan(summary="My plan.", n_steps=3),
    )
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="moderate", reasoning="r"),
        ),
        planner=planner,
    )
    text = await router.handle(ctx=_ctx(), thread_id=uuid4(), content="x")
    # Preview should mention Executor missing, the summary, and each step.
    assert "Executor isn't online yet" in text
    assert "My plan." in text
    assert "step 0" in text
    assert "step 2" in text


async def test_plan_route_emits_plan_event():
    emitted: list[tuple[str, str]] = []

    async def emit(event, data):
        emitted.append((event, data))

    planner = FakePlanner(
        plan=_plan(n_steps=2, skill="vendor_comparison_spreadsheet"),
    )
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="moderate", reasoning="r"),
        ),
        planner=planner,
    )
    await router.handle(
        ctx=_ctx(), thread_id=uuid4(), content="x", emit=emit,
    )
    plan_events = [e for e in emitted if e[0] == "plan"]
    assert len(plan_events) == 1
    summary = plan_events[0][1]
    assert "2 steps" in summary
    assert "vendor_comparison_spreadsheet" in summary


async def test_render_plan_preview_includes_task_hint_when_is_task():
    text = _render_plan_preview(_plan(is_task=True))
    assert "Task" in text
