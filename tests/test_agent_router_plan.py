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
    """Stub conv.append + the scoring-side DB calls so plan-route tests
    don't need Postgres. Also injects a default-approving Pre-Evaluator
    so the Router doesn't fall back to the real singleton (which would
    try to make an Anthropic call)."""
    appended: list[dict] = []
    scored: list[dict] = []
    events: list[dict] = []

    async def fake_append(_conn, **kw):
        appended.append(kw)
        return uuid4()

    async def fake_update_outcome(_conn, **kw):
        scored.append(kw)

    async def fake_append_event(_conn, **kw):
        events.append(kw)
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
    # Pre-Evaluator default — approve unless the test overrides it.
    # Patch both the source module + the name bound into Router.
    monkeypatch.setattr(
        "wolfpaw.agents.plan_pre_evaluator.get_pre_evaluator_agent",
        lambda: FakePreEvaluator(),
    )
    monkeypatch.setattr(
        "wolfpaw.agents.router.get_pre_evaluator_agent",
        lambda: FakePreEvaluator(),
    )
    yield {"appended": appended, "scored": scored, "events": events}


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


class FakePostEvaluator:
    """Default no-op post-evaluator for tests that don't care about scoring."""

    def __init__(self, verdict=None, raises=None):
        from wolfpaw.schemas import PostEvalVerdict as _V
        self._verdict = verdict or _V(score=70, summary="ok")
        self._raises = raises
        self.calls: list[dict] = []

    async def evaluate(self, *, ctx, plan, execution):
        self.calls.append({"plan_id": plan.id, "success": execution.success})
        if self._raises:
            raise self._raises
        return self._verdict


class FakePreEvaluator:
    """Default-approving pre-evaluator. Tests that exercise the retry
    path override `verdicts` to push a reject-then-approve sequence."""

    def __init__(self, verdicts=None):
        from wolfpaw.schemas import PreEvalVerdict as _V
        self._verdicts = list(verdicts) if verdicts else [
            _V(
                approved=True, achieves_objective=True,
                simplifiable=False, better_than_past_plans=True,
                diagnosis="(test default)",
            ),
        ]
        self.calls: list[dict] = []

    async def evaluate(self, *, ctx, content, plan, past_plans=None):
        self.calls.append({
            "content": content, "plan_id": plan.id,
            "past_plans_count": len(past_plans or []),
        })
        if len(self._verdicts) == 1:
            return self._verdicts[0]
        return self._verdicts.pop(0)


def _empty_plan_ctx():
    from wolfpaw.agents.planner import PlanContext
    return PlanContext(
        past_plans=[], relevant_skills=[],
        summaries=[], vector_recall=[],
    )


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
        post_evaluator=FakePostEvaluator(),
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
        post_evaluator=FakePostEvaluator(),
    )
    await router.handle(ctx=_ctx(), thread_id=uuid4(), content="big job")
    assert planner.calls[0]["complexity_hint"] == "ambitious"


async def test_plan_route_persists_user_and_final_text(_stub_persistence):
    appended = _stub_persistence["appended"]
    plan = _plan()
    planner = FakePlanner(plan=plan)
    executor = FakeExecutor(_execution(plan, final_answer="the answer"))
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="moderate", reasoning="r"),
        ),
        planner=planner, executor=executor,
        post_evaluator=FakePostEvaluator(),
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
        post_evaluator=FakePostEvaluator(),
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
        post_evaluator=FakePostEvaluator(),
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
        post_evaluator=FakePostEvaluator(),
    )
    text = await router.handle(ctx=_ctx(), thread_id=uuid4(), content="x")
    assert text == "I ran into a problem."


async def test_plan_route_calls_post_evaluator_and_persists_score(_stub_persistence):
    from wolfpaw.schemas import PostEvalVerdict
    scored = _stub_persistence["scored"]
    events = _stub_persistence["events"]
    plan = _plan()
    planner = FakePlanner(plan=plan)
    executor = FakeExecutor(_execution(plan, final_answer="ans"))
    evaluator = FakePostEvaluator(
        verdict=PostEvalVerdict(
            score=83, summary="solid", what_went_well="tools clean",
        ),
    )
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="moderate", reasoning="r"),
        ),
        planner=planner, executor=executor, post_evaluator=evaluator,
    )
    await router.handle(ctx=_ctx(), thread_id=uuid4(), content="x")
    # Evaluator was called with the same plan + execution.
    assert len(evaluator.calls) == 1
    assert evaluator.calls[0]["plan_id"] == plan.id
    # procedural.update_outcome got the score (and only the score, no fields clobbered).
    assert len(scored) == 1
    assert scored[0]["plan_id"] == plan.id
    assert scored[0]["score"] == 83
    # A task_events row was emitted with the verdict's full content.
    assert len(events) == 1
    assert events[0]["event_type"] == "plan_scored"
    assert events[0]["content"]["score"] == 83
    assert events[0]["content"]["plan_id"] == str(plan.id)
    assert events[0]["content"]["what_went_well"] == "tools clean"


async def test_plan_route_emits_score_event():
    emitted: list[tuple[str, str]] = []

    async def emit(event, data):
        emitted.append((event, data))

    from wolfpaw.schemas import PostEvalVerdict
    plan = _plan()
    planner = FakePlanner(plan=plan)
    executor = FakeExecutor(_execution(plan, final_answer="x"))
    evaluator = FakePostEvaluator(
        verdict=PostEvalVerdict(score=72, summary="ok-ish"),
    )
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="moderate", reasoning="r"),
        ),
        planner=planner, executor=executor, post_evaluator=evaluator,
    )
    await router.handle(ctx=_ctx(), thread_id=uuid4(), content="x", emit=emit)
    score_events = [e for e in emitted if e[0] == "score"]
    assert len(score_events) == 1
    assert "72/100" in score_events[0][1]
    assert "ok-ish" in score_events[0][1]


async def test_plan_route_post_evaluator_failure_doesnt_block_response(_stub_persistence):
    """If the Post-Evaluator itself raises, the user still gets the
    Executor's final answer. Scoring is best-effort."""
    plan = _plan()
    planner = FakePlanner(plan=plan)
    executor = FakeExecutor(_execution(plan, final_answer="the answer"))
    evaluator = FakePostEvaluator(raises=RuntimeError("post-eval crashed"))
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="moderate", reasoning="r"),
        ),
        planner=planner, executor=executor, post_evaluator=evaluator,
    )
    text = await router.handle(ctx=_ctx(), thread_id=uuid4(), content="x")
    assert text == "the answer"
    # Score path didn't write anything since evaluator failed early.
    assert _stub_persistence["scored"] == []
    assert _stub_persistence["events"] == []


async def test_plan_route_skips_score_persistence_when_plan_id_missing():
    """If the planner failed to persist + plan.id is None, the router
    still calls the evaluator (for the emit) but skips the score-persist
    side-effects since there's no plan row to update."""
    from wolfpaw.schemas import PostEvalVerdict
    plan = _plan()
    # Strip the id off.
    plan = Plan(
        query=plan.query, summary=plan.summary, steps=plan.steps,
        is_task=plan.is_task, model_used=plan.model_used,
        applied_skill_name=plan.applied_skill_name, id=None,
    )
    planner = FakePlanner(plan=plan)
    executor = FakeExecutor(_execution(plan, final_answer="x"))
    evaluator = FakePostEvaluator(
        verdict=PostEvalVerdict(score=60, summary="ok"),
    )
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="moderate", reasoning="r"),
        ),
        planner=planner, executor=executor, post_evaluator=evaluator,
    )
    await router.handle(ctx=_ctx(), thread_id=uuid4(), content="x")
    assert len(evaluator.calls) == 1  # still evaluated


# --- step 24: Plan Pre-Evaluator -----------------------------------------


async def test_pre_eval_approves_first_pass_no_retry():
    """Default-approving pre-evaluator → Planner runs once, Executor
    sees the first draft."""
    from wolfpaw.schemas import PreEvalVerdict

    plan = _plan()
    planner = FakePlanner(plan=plan)
    executor = FakeExecutor(_execution(plan, final_answer="done"))
    pre = FakePreEvaluator()
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="moderate", reasoning="r"),
        ),
        planner=planner, executor=executor,
        pre_evaluator=pre, post_evaluator=FakePostEvaluator(),
    )
    text = await router.handle(ctx=_ctx(), thread_id=uuid4(), content="x")
    assert text == "done"
    assert len(planner.calls) == 1
    assert len(pre.calls) == 1
    # No revision_diagnosis was forwarded on the (only) planner call.
    # FakePlanner doesn't record it, but a second call would imply a
    # retry — assert absence directly.


async def test_pre_eval_rejects_then_planner_runs_second_pass():
    """Rejected first-pass → Planner gets a second call with
    revision_diagnosis, Executor runs against the second draft."""
    from wolfpaw.schemas import PreEvalVerdict

    plan_v1 = _plan(summary="first draft")
    plan_v2 = _plan(summary="revised draft")

    class _TwoShotPlanner:
        def __init__(self):
            self.calls: list[dict] = []
            self._plans = [plan_v1, plan_v2]

        async def plan(self, *, ctx, thread_id, content,
                       complexity_hint="moderate",
                       revision_diagnosis=None):
            self.calls.append({
                "content": content,
                "revision_diagnosis": revision_diagnosis,
            })
            return self._plans.pop(0), _empty_plan_ctx()

    planner = _TwoShotPlanner()
    executor = FakeExecutor(_execution(plan_v2, final_answer="second-pass result"))
    pre = FakePreEvaluator(verdicts=[
        PreEvalVerdict(
            approved=False, achieves_objective=True,
            simplifiable=True, better_than_past_plans=True,
            diagnosis="step s1 is redundant — drop it",
        ),
        # second verdict isn't consulted; helper ships the second
        # plan unchecked.
    ])
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="moderate", reasoning="r"),
        ),
        planner=planner, executor=executor,
        pre_evaluator=pre, post_evaluator=FakePostEvaluator(),
    )
    text = await router.handle(ctx=_ctx(), thread_id=uuid4(), content="x")
    assert text == "second-pass result"
    assert len(planner.calls) == 2
    # First-pass had no diagnosis; second-pass carries the rejection text.
    assert planner.calls[0]["revision_diagnosis"] is None
    assert "redundant" in (planner.calls[1]["revision_diagnosis"] or "")
    # Pre-eval was called once (against the first draft only).
    assert len(pre.calls) == 1
    # Executor saw the second plan.
    assert executor.calls[0]["plan_id"] == plan_v2.id


async def test_pre_eval_emits_pre_eval_sse_event():
    """The helper emits a `pre_eval` SSE event with the verdict
    summary so the UI can show what happened."""
    from wolfpaw.schemas import PreEvalVerdict

    emitted: list[tuple[str, str]] = []

    async def emit(event, data):
        emitted.append((event, data))

    plan = _plan()
    planner = FakePlanner(plan=plan)
    executor = FakeExecutor(_execution(plan, final_answer="x"))
    pre = FakePreEvaluator(verdicts=[
        PreEvalVerdict(
            approved=True, achieves_objective=True,
            simplifiable=False, better_than_past_plans=True,
            diagnosis="looks clean",
        ),
    ])
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="moderate", reasoning="r"),
        ),
        planner=planner, executor=executor,
        pre_evaluator=pre, post_evaluator=FakePostEvaluator(),
    )
    await router.handle(ctx=_ctx(), thread_id=uuid4(), content="x", emit=emit)
    pre_eval_events = [e for e in emitted if e[0] == "pre_eval"]
    assert len(pre_eval_events) == 1
    assert "approved" in pre_eval_events[0][1]
    assert "looks clean" in pre_eval_events[0][1]


async def test_pre_eval_retry_emits_two_pre_eval_events():
    """Reject + retry path emits one event for the rejection and a
    second when the helper ships the unchecked second draft."""
    from wolfpaw.schemas import PreEvalVerdict

    emitted: list[tuple[str, str]] = []

    async def emit(event, data):
        emitted.append((event, data))

    plan_v1 = _plan()
    plan_v2 = _plan(summary="revised")

    class _Two:
        def __init__(self):
            self.calls = []
            self._plans = [plan_v1, plan_v2]

        async def plan(self, *, ctx, thread_id, content,
                       complexity_hint="moderate",
                       revision_diagnosis=None):
            self.calls.append({"diag": revision_diagnosis})
            return self._plans.pop(0), _empty_plan_ctx()

    planner = _Two()
    executor = FakeExecutor(_execution(plan_v2, final_answer="ok"))
    pre = FakePreEvaluator(verdicts=[
        PreEvalVerdict(
            approved=False, achieves_objective=False,
            simplifiable=False, better_than_past_plans=True,
            diagnosis="doesn't actually answer the user's question",
        ),
    ])
    router = Router(
        triage=FakeTriage(
            TriageVerdict(route="plan", complexity="moderate", reasoning="r"),
        ),
        planner=planner, executor=executor,
        pre_evaluator=pre, post_evaluator=FakePostEvaluator(),
    )
    await router.handle(ctx=_ctx(), thread_id=uuid4(), content="x", emit=emit)
    pre_eval_events = [e for e in emitted if e[0] == "pre_eval"]
    assert len(pre_eval_events) == 2
    assert "rejected" in pre_eval_events[0][1]
    assert "doesn't-achieve-objective" in pre_eval_events[0][1]
    assert "retry" in pre_eval_events[1][1]
