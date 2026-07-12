"""Router tests for the plan-driven task path.

The task routing used to come from Triage emitting `route="task"`.
That's gone — Triage now emits only `quick` or `plan`, and the
Planner decides via `plan.is_task` whether the plan needs the Task
lifecycle. The Router reads `plan.is_task` after Pre-Eval to choose
between inline execution and Task creation."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from wolfpaw.agents.planner import PlanContext
from wolfpaw.agents.router import Router, _title_from_content
from wolfpaw.agents.triage import TriageVerdict
from wolfpaw.memory.tasks import Task
from wolfpaw.schemas import (
    ExecutionPlan, Plan, PostEvalVerdict, Step, StepResult, StepStatus,
)
from wolfpaw.tasks.service import TaskOutcome
from wolfpaw.toolbox.registry import ToolContext


@asynccontextmanager
async def _fake_acquire():
    yield None


@pytest.fixture(autouse=True)
def _stub_persistence(monkeypatch):
    """conv.append goes to an in-memory list. Pre-Evaluator is
    bypassed by injecting an always-approving fake (the model-side
    pre-eval is exercised in its own test module).

    Also forces WOLFPAW_WORKERS_ENABLED=false per test so a test that
    flips it to true (workers-on case) doesn't leak into subsequent
    tests via the lru_cache."""
    from wolfpaw.config import get_settings

    monkeypatch.setenv("WOLFPAW_WORKERS_ENABLED", "false")
    get_settings.cache_clear()  # type: ignore[attr-defined]

    appended: list[dict] = []

    async def fake_append(_conn, **kw):
        appended.append(kw)
        return uuid4()

    async def fake_update_outcome(_conn, **_kw):
        return None

    async def fake_append_event(_conn, **_kw):
        return uuid4()

    monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.agents.router.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.agents.router.conv.append", fake_append)
    monkeypatch.setattr(
        "wolfpaw.agents.router.procedural.update_outcome", fake_update_outcome,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.router.task_events.append_event", fake_append_event,
    )

    from wolfpaw.schemas import PreEvalVerdict

    class _ApprovingPreEval:
        async def evaluate(self, *, ctx, content, plan, past_plans=None,
                           human_available=True):
            return PreEvalVerdict(
                approved=True, achieves_objective=True,
                simplifiable=False, better_than_past_plans=True,
                diagnosis="ok",
            )

    monkeypatch.setattr(
        "wolfpaw.agents.router.get_pre_evaluator_agent",
        lambda: _ApprovingPreEval(),
    )

    async def fake_maybe(*, ctx, plan, execution, verdict, emit=None):
        return None

    monkeypatch.setattr(
        "wolfpaw.agents.router.maybe_distill_skill", fake_maybe,
    )
    yield appended


# --- fakes ---------------------------------------------------------------


@dataclass
class FakeTriage:
    verdict: TriageVerdict

    async def classify(self, *, ctx, thread_id, content):
        return self.verdict


def _empty_plan_ctx() -> PlanContext:
    return PlanContext(
        past_plans=[], relevant_skills=[],
        summaries=[], vector_recall=[],
    )


def _plan(*, is_task: bool, summary: str = "do stuff") -> Plan:
    return Plan(
        query="x", summary=summary,
        steps=[Step(id="s1", kind="reasoning", description="think")],
        is_task=is_task, model_used="claude-sonnet-4-6",
        id=uuid4(),
    )


@dataclass
class FakePlanner:
    plan_obj: Plan

    async def plan(self, *, ctx, thread_id, content, complexity_hint="moderate",
                   revision_diagnosis=None, replan_from=None,
                   human_available=True):
        return self.plan_obj, _empty_plan_ctx()


class FakeExecutor:
    def __init__(self, final_answer: str = "inline done"):
        self.calls: list[dict] = []
        self._final = final_answer

    async def execute(self, *, ctx, plan, emit=None):
        self.calls.append({"plan_id": plan.id})
        return ExecutionPlan(
            plan=plan,
            results=[
                StepResult(
                    step_id=s.id, kind=s.kind,
                    status=StepStatus.COMPLETED, output="ok",
                )
                for s in plan.steps
            ],
            final_answer=self._final, success=True,
        )


class FakePostEvaluator:
    async def evaluate(self, *, ctx, plan, execution):
        return PostEvalVerdict(score=70, summary="ok")


class FakeTaskService:
    """Records create/create_and_run calls. ``run`` should never be
    called from the Router (workers-off path uses create_and_run;
    workers-on path uses create + enqueue)."""

    def __init__(self, *, final_answer: str = "task done."):
        self.create_calls: list[dict] = []
        self.create_and_run_calls: list[dict] = []
        self._final = final_answer

    async def create(self, **kw):
        self.create_calls.append(kw)
        return self._mk_task(kw, status="pending")

    async def create_and_run(self, **kw):
        self.create_and_run_calls.append(kw)
        emit = kw.get("emit")
        if emit is not None:
            await emit("task", "fake-task-id")
        return TaskOutcome(
            task=self._mk_task(kw, status="completed"),
            plan=kw.get("precomputed_plan"),
            execution=None, verdict=None,
            final_answer=self._final,
        )

    def _mk_task(self, kw: dict, *, status: str) -> Task:
        return Task(
            id=uuid4(), user_id=kw.get("user_id") or uuid4(),
            parent_task_id=None,
            title=kw.get("title", ""), description=kw.get("description"),
            status=status, current_plan_id=None,
            budget_cents=kw.get("budget_cents"), spent_cents=0,
            blocking_reason=None,
            channel_for_completion=kw.get("channel_for_completion"),
            schedule_pattern=None,
            created_at=datetime.now(timezone.utc),
            started_at=None, completed_at=None, last_active_at=None,
        )


def _ctx():
    return ToolContext(user_id=uuid4())


# --- title helpers --------------------------------------------------------


def test_title_from_content_uses_first_line():
    assert _title_from_content("research vendors\n\ndetails here") == "research vendors"


def test_title_from_content_caps_long_first_line():
    long = "x" * 200
    title = _title_from_content(long)
    assert len(title) <= 80
    assert title.endswith("…")


def test_title_from_content_empty_falls_back():
    assert _title_from_content("   ") == "Task"


# --- plan-driven routing -------------------------------------------------


async def test_plan_with_is_task_false_runs_inline_no_task_created():
    """Planner emits is_task=false → Router executes inline, never
    touches TaskService."""
    plan = _plan(is_task=False)
    planner = FakePlanner(plan_obj=plan)
    executor = FakeExecutor(final_answer="inline result")
    svc = FakeTaskService()

    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="moderate", reasoning="r"),
        ),
        planner=planner, executor=executor,
        post_evaluator=FakePostEvaluator(),
        task_service=svc,
    )
    text = await router.handle(ctx=_ctx(), thread_id=uuid4(), content="x")
    assert text == "inline result"
    assert len(executor.calls) == 1
    assert svc.create_calls == []
    assert svc.create_and_run_calls == []


async def test_plan_with_is_task_true_routes_to_task_when_workers_off(monkeypatch):
    """Planner emits is_task=true + workers off → Router calls
    task_service.create_and_run with the precomputed plan (saves the
    Sonnet call that re-planning would burn)."""
    from wolfpaw.config import get_settings

    monkeypatch.setenv("WOLFPAW_WORKERS_ENABLED", "false")
    get_settings.cache_clear()  # type: ignore[attr-defined]

    plan = _plan(is_task=True, summary="multi-day watch")
    planner = FakePlanner(plan_obj=plan)
    svc = FakeTaskService(final_answer="task synthesized")

    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="ambitious", reasoning="r"),
        ),
        planner=planner, executor=FakeExecutor(),
        post_evaluator=FakePostEvaluator(),
        task_service=svc,
    )
    text = await router.handle(
        ctx=_ctx(), thread_id=uuid4(),
        content="monitor stock XYZ and alert me when it crosses $100",
    )
    # Inline execution went through TaskService (which uses the
    # precomputed plan), not the Router's own executor.
    assert text == "task synthesized"
    assert len(svc.create_and_run_calls) == 1
    call = svc.create_and_run_calls[0]
    assert call["precomputed_plan"] is plan
    assert call["channel_for_completion"] == "web"


async def test_plan_with_is_task_true_routes_to_task_when_workers_on(monkeypatch):
    """Planner emits is_task=true + workers on → Router calls
    task_service.create() + enqueues the run + returns "Started Task"
    ack. The arq worker re-plans inside the task (acceptable cost on
    the worker hop)."""
    from wolfpaw.config import get_settings

    monkeypatch.setenv("WOLFPAW_WORKERS_ENABLED", "true")
    get_settings.cache_clear()  # type: ignore[attr-defined]

    enqueued: list = []

    async def fake_enqueue_run_task(task_id):
        enqueued.append(task_id)

    monkeypatch.setattr(
        "wolfpaw.workers.queue.enqueue_run_task", fake_enqueue_run_task,
    )

    plan = _plan(is_task=True)
    planner = FakePlanner(plan_obj=plan)
    svc = FakeTaskService()

    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="ambitious", reasoning="r"),
        ),
        planner=planner, executor=FakeExecutor(),
        post_evaluator=FakePostEvaluator(),
        task_service=svc,
    )
    text = await router.handle(
        ctx=_ctx(), thread_id=uuid4(),
        content="watch X long-term",
    )
    assert len(svc.create_calls) == 1
    assert svc.create_and_run_calls == []
    assert len(enqueued) == 1
    assert "Started Task" in text
    assert str(enqueued[0]) in text


async def test_plan_task_path_persists_user_and_ack_or_answer(_stub_persistence):
    """Both inline and deferred task paths persist the user message +
    the assistant-side result (either the synthesized answer or the
    'Started Task' ack)."""
    plan = _plan(is_task=True)
    planner = FakePlanner(plan_obj=plan)
    svc = FakeTaskService(final_answer="result")

    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="moderate", reasoning="r"),
        ),
        planner=planner, executor=FakeExecutor(),
        post_evaluator=FakePostEvaluator(),
        task_service=svc,
    )
    await router.handle(ctx=_ctx(), thread_id=uuid4(), content="watch X")
    appended = _stub_persistence
    roles = [a["role"] for a in appended]
    contents = [a["content"] for a in appended]
    assert roles == ["user", "assistant"]
    assert contents == ["watch X", "result"]


def _plan_hitl() -> Plan:
    """A task plan that pauses on `ask_user` — `_requires_task_context`
    picks it up via the ask_user tool's `requires_task_context` flag."""
    return Plan(
        query="x", summary="ask then act",
        steps=[
            Step(
                id="s1", kind="functional", tool="ask_user",
                description="ask the user for a URL",
                inputs={"question": "what's the URL?"},
            ),
            Step(id="s2", kind="reasoning", description="summarize the site"),
        ],
        is_task=True, model_used="claude-sonnet-4-6", id=uuid4(),
    )


async def test_hitl_plan_stays_inline_on_web_even_with_workers_on(monkeypatch):
    """A plan with an `ask_user` step must NOT background on a streaming
    channel (web, emit present): the question is delivered through the
    live SSE stream, which a backgrounded task doesn't have. Router calls
    create_and_run (not create + enqueue) and threads the originating
    channel through for delivery."""
    from wolfpaw.config import get_settings

    monkeypatch.setenv("WOLFPAW_WORKERS_ENABLED", "true")
    get_settings.cache_clear()  # type: ignore[attr-defined]

    enqueued: list = []

    async def fake_enqueue_run_task(task_id):
        enqueued.append(task_id)

    monkeypatch.setattr(
        "wolfpaw.workers.queue.enqueue_run_task", fake_enqueue_run_task,
    )

    svc = FakeTaskService(final_answer="asked and answered")
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="moderate", reasoning="r"),
        ),
        planner=FakePlanner(plan_obj=_plan_hitl()), executor=FakeExecutor(),
        post_evaluator=FakePostEvaluator(),
        task_service=svc,
    )

    async def emit(event, data):
        pass

    ctx = ToolContext(user_id=uuid4(), channel="web")
    text = await router.handle(
        ctx=ctx, thread_id=uuid4(),
        content="ask me for a URL then summarize it", emit=emit,
    )
    assert enqueued == []
    assert svc.create_calls == []
    assert len(svc.create_and_run_calls) == 1
    assert svc.create_and_run_calls[0]["channel"] == "web"
    assert text == "asked and answered"


async def test_hitl_plan_backgrounds_on_push_channel(monkeypatch):
    """On a push channel (Telegram: emit=None) a HITL plan backgrounds
    normally — `ask_user` delivers via the channel's send(). The
    originating channel is still threaded into create() so the worker
    can reach the user."""
    from wolfpaw.config import get_settings

    monkeypatch.setenv("WOLFPAW_WORKERS_ENABLED", "true")
    get_settings.cache_clear()  # type: ignore[attr-defined]

    enqueued: list = []

    async def fake_enqueue_run_task(task_id):
        enqueued.append(task_id)

    monkeypatch.setattr(
        "wolfpaw.workers.queue.enqueue_run_task", fake_enqueue_run_task,
    )

    svc = FakeTaskService()
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="moderate", reasoning="r"),
        ),
        planner=FakePlanner(plan_obj=_plan_hitl()), executor=FakeExecutor(),
        post_evaluator=FakePostEvaluator(),
        task_service=svc,
    )
    ctx = ToolContext(user_id=uuid4(), channel="telegram")
    await router.handle(
        ctx=ctx, thread_id=uuid4(),
        content="ask me for a URL then summarize it", emit=None,
    )
    assert svc.create_and_run_calls == []
    assert len(svc.create_calls) == 1
    assert svc.create_calls[0]["channel"] == "telegram"
    assert len(enqueued) == 1


async def test_plan_task_path_propagates_emit_callback():
    """The emit hook is propagated into TaskService so the SSE stream
    surfaces the `task` event."""
    emitted: list[tuple[str, str]] = []

    async def emit(event, data):
        emitted.append((event, data))

    plan = _plan(is_task=True)
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="moderate", reasoning="r"),
        ),
        planner=FakePlanner(plan_obj=plan),
        executor=FakeExecutor(),
        post_evaluator=FakePostEvaluator(),
        task_service=FakeTaskService(),
    )
    await router.handle(ctx=_ctx(), thread_id=uuid4(), content="x", emit=emit)
    kinds = [e for e, _ in emitted]
    assert "triage" in kinds
    assert "plan" in kinds
    assert "task" in kinds  # FakeTaskService emits this on create_and_run
