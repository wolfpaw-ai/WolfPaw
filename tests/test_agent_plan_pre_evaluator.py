"""Plan Pre-Evaluator unit tests — forced-tool parsing, approval
logic, retry helper, failure posture."""

from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Iterable
from uuid import uuid4

import pytest

from wolfpaw.agents.plan_pre_evaluator import (
    PlanPreEvaluatorAgent,
    plan_with_pre_evaluation,
)
from wolfpaw.memory import procedural
from wolfpaw.metering.model_client import ModelClient
from wolfpaw.schemas import Plan, PreEvalVerdict, Step
from wolfpaw.toolbox.registry import ToolContext


@asynccontextmanager
async def _fake_acquire():
    yield None


class FakeAnthropic:
    def __init__(self, turns: Iterable[Any]) -> None:
        self._turns = list(turns)
        self.calls: list[dict] = []
        self.messages = self

    async def create(self, **kwargs):
        snapshot = dict(kwargs)
        if "messages" in snapshot:
            snapshot["messages"] = copy.deepcopy(snapshot["messages"])
        self.calls.append(snapshot)
        return self._turns.pop(0)


def _eval_response(
    *,
    achieves_objective: bool,
    simplifiable: bool,
    better_than_past_plans: bool,
    diagnosis: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use", name="evaluate_plan", id="tu_eval",
                input={
                    "achieves_objective": achieves_objective,
                    "simplifiable": simplifiable,
                    "better_than_past_plans": better_than_past_plans,
                    "diagnosis": diagnosis,
                },
            ),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=20, output_tokens=15,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )


def _plan() -> Plan:
    return Plan(
        query="research vendors",
        summary="research five vendors and emit a table",
        steps=[
            # Schema-valid by construction so the new mechanical
            # validator passes — these tests are about the *model*
            # side of pre-eval, not the structural check.
            Step(
                id="s1", kind="functional", description="fetch",
                tool="web_search", inputs={"query": "vendor research"},
            ),
            Step(id="s2", kind="reasoning", description="summarize"),
        ],
        is_task=False,
        model_used="claude-sonnet-4-6",
        id=uuid4(),
    )


@pytest.fixture
def eval_env(monkeypatch):
    """Stub the DB hooks ModelClient + PlanPreEvaluatorAgent reach."""
    async def fake_bump(_conn, *, agent, version_label, content_template):
        return SimpleNamespace(
            id=uuid4(), agent=agent, version_label=version_label,
            content_hash="fake", content_template=content_template,
        )

    async def fake_record(*args, **kwargs):
        return uuid4()

    async def fake_price(*args, **kwargs):
        return None

    monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
    monkeypatch.setattr(
        "wolfpaw.agents.plan_pre_evaluator.acquire", _fake_acquire,
    )
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.acquire", _fake_acquire,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.plan_pre_evaluator.bump_prompt_version", fake_bump,
    )
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.record_usage", fake_record,
    )
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.get_active_price", fake_price,
    )


# --- forced-tool parsing --------------------------------------------------


async def test_evaluate_returns_approved_when_all_three_checks_pass(eval_env):
    fake_anthropic = FakeAnthropic([
        _eval_response(
            achieves_objective=True, simplifiable=False,
            better_than_past_plans=True, diagnosis="looks good",
        ),
    ])
    agent = PlanPreEvaluatorAgent(
        model_client=ModelClient(anthropic=fake_anthropic),
    )
    verdict = await agent.evaluate(
        ctx=ToolContext(user_id=uuid4()), content="q", plan=_plan(),
    )
    assert verdict.approved is True
    assert verdict.diagnosis == "looks good"


async def test_evaluate_rejects_on_unachieved_objective(eval_env):
    fake_anthropic = FakeAnthropic([
        _eval_response(
            achieves_objective=False, simplifiable=False,
            better_than_past_plans=True,
            diagnosis="plan never produces the spreadsheet the user asked for",
        ),
    ])
    agent = PlanPreEvaluatorAgent(
        model_client=ModelClient(anthropic=fake_anthropic),
    )
    verdict = await agent.evaluate(
        ctx=ToolContext(user_id=uuid4()), content="q", plan=_plan(),
    )
    assert verdict.approved is False
    assert verdict.achieves_objective is False
    assert "spreadsheet" in verdict.diagnosis


async def test_evaluate_rejects_on_simplifiable(eval_env):
    fake_anthropic = FakeAnthropic([
        _eval_response(
            achieves_objective=True, simplifiable=True,
            better_than_past_plans=True, diagnosis="step s3 is redundant",
        ),
    ])
    agent = PlanPreEvaluatorAgent(
        model_client=ModelClient(anthropic=fake_anthropic),
    )
    verdict = await agent.evaluate(
        ctx=ToolContext(user_id=uuid4()), content="q", plan=_plan(),
    )
    assert verdict.approved is False
    assert verdict.simplifiable is True


async def test_evaluate_rejects_on_worse_than_past_plan(eval_env):
    fake_anthropic = FakeAnthropic([
        _eval_response(
            achieves_objective=True, simplifiable=False,
            better_than_past_plans=False,
            diagnosis="past plan abc123 (score=95) handled this in 3 steps",
        ),
    ])
    agent = PlanPreEvaluatorAgent(
        model_client=ModelClient(anthropic=fake_anthropic),
    )
    verdict = await agent.evaluate(
        ctx=ToolContext(user_id=uuid4()), content="q", plan=_plan(),
    )
    assert verdict.approved is False
    assert verdict.better_than_past_plans is False


async def test_evaluate_forces_evaluate_plan_tool_use(eval_env):
    fake_anthropic = FakeAnthropic([
        _eval_response(
            achieves_objective=True, simplifiable=False,
            better_than_past_plans=True, diagnosis="",
        ),
    ])
    agent = PlanPreEvaluatorAgent(
        model_client=ModelClient(anthropic=fake_anthropic),
    )
    await agent.evaluate(
        ctx=ToolContext(user_id=uuid4()), content="q", plan=_plan(),
    )
    call = fake_anthropic.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "evaluate_plan"}
    assert any(t["name"] == "evaluate_plan" for t in call["tools"])


async def test_evaluate_inlines_high_scoring_past_plans_into_prompt(eval_env):
    fake_anthropic = FakeAnthropic([
        _eval_response(
            achieves_objective=True, simplifiable=False,
            better_than_past_plans=True, diagnosis="ok",
        ),
    ])
    agent = PlanPreEvaluatorAgent(
        model_client=ModelClient(anthropic=fake_anthropic),
    )
    past = [
        procedural.StoredPlan(
            id=uuid4(), user_id=uuid4(), thread_id=None, task_id=None,
            query="prior research",
            steps=[{"id": "p1", "kind": "reasoning", "description": "think"}],
            score=95, similarity=0.9,
        ),
        # Low-scored past plan should be filtered out — only score ≥ 80 surfaces.
        procedural.StoredPlan(
            id=uuid4(), user_id=uuid4(), thread_id=None, task_id=None,
            query="weak prior", steps=[], score=40, similarity=0.7,
        ),
    ]
    await agent.evaluate(
        ctx=ToolContext(user_id=uuid4()), content="q",
        plan=_plan(), past_plans=past,
    )
    user_msg = fake_anthropic.calls[0]["messages"][0]["content"]
    assert "prior research" in user_msg
    assert "weak prior" not in user_msg


# --- failure posture (approve-by-default on errors) -----------------------


async def test_evaluate_approves_by_default_when_model_call_fails(eval_env):
    class _BrokenAnthropic:
        def __init__(self):
            self.messages = self

        async def create(self, **_kw):
            raise RuntimeError("anthropic down")

    agent = PlanPreEvaluatorAgent(
        model_client=ModelClient(anthropic=_BrokenAnthropic()),
    )
    verdict = await agent.evaluate(
        ctx=ToolContext(user_id=uuid4()), content="q", plan=_plan(),
    )
    assert verdict.approved is True
    assert "model call failed" in verdict.diagnosis


async def test_evaluate_approves_by_default_when_no_tool_use_returned(eval_env):
    """A turn with stop_reason='end_turn' (no tool_use) is degenerate
    — approve so the user still gets an answer."""
    no_tool_response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="I refuse")],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=5,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )
    fake_anthropic = FakeAnthropic([no_tool_response])
    agent = PlanPreEvaluatorAgent(
        model_client=ModelClient(anthropic=fake_anthropic),
    )
    verdict = await agent.evaluate(
        ctx=ToolContext(user_id=uuid4()), content="q", plan=_plan(),
    )
    assert verdict.approved is True
    assert "no tool_use" in verdict.diagnosis


# --- plan_with_pre_evaluation retry helper --------------------------------


class _FakePlannerForHelper:
    def __init__(self, plans):
        self._plans = list(plans)
        self.calls: list[dict] = []

    async def plan(self, *, ctx, thread_id, content,
                   complexity_hint="moderate", revision_diagnosis=None):
        self.calls.append({
            "content": content,
            "revision_diagnosis": revision_diagnosis,
        })
        from wolfpaw.agents.planner import PlanContext

        plan_ctx = PlanContext(
            past_plans=[], relevant_skills=[],
            summaries=[], vector_recall=[],
        )
        return self._plans.pop(0), plan_ctx


@dataclass
class _FakePreEvaluator:
    verdicts: list[PreEvalVerdict]
    calls: list[dict] | None = None

    def __post_init__(self):
        self.calls = []

    async def evaluate(self, *, ctx, content, plan, past_plans=None):
        self.calls.append({"plan_summary": plan.summary})
        return self.verdicts.pop(0)


async def test_plan_with_pre_evaluation_approve_path_skips_retry():
    p1 = _plan()
    planner = _FakePlannerForHelper([p1])
    pre = _FakePreEvaluator(verdicts=[
        PreEvalVerdict(
            approved=True, achieves_objective=True,
            simplifiable=False, better_than_past_plans=True,
            diagnosis="clean",
        ),
    ])
    plan, verdict, retried = await plan_with_pre_evaluation(
        planner=planner, pre_evaluator=pre,
        ctx=ToolContext(user_id=uuid4()), thread_id=None,
        content="q", complexity_hint="moderate",
    )
    assert plan is p1
    assert verdict.approved is True
    assert retried is False
    assert len(planner.calls) == 1
    assert planner.calls[0]["revision_diagnosis"] is None


async def test_plan_with_pre_evaluation_rejected_then_retried():
    p1 = _plan()
    p2 = Plan(
        query=p1.query, summary="v2", steps=p1.steps,
        is_task=False, model_used=p1.model_used, id=uuid4(),
    )
    planner = _FakePlannerForHelper([p1, p2])
    pre = _FakePreEvaluator(verdicts=[
        PreEvalVerdict(
            approved=False, achieves_objective=True,
            simplifiable=True, better_than_past_plans=True,
            diagnosis="too many steps",
        ),
    ])
    plan, verdict, retried = await plan_with_pre_evaluation(
        planner=planner, pre_evaluator=pre,
        ctx=ToolContext(user_id=uuid4()), thread_id=None,
        content="q", complexity_hint="moderate",
    )
    assert plan is p2
    assert retried is True
    # The first-pass verdict is what gets returned, even though the
    # second-pass plan is what gets shipped.
    assert verdict.approved is False
    assert "too many steps" in verdict.diagnosis
    assert len(planner.calls) == 2
    assert planner.calls[1]["revision_diagnosis"] == "too many steps"


async def test_evaluate_short_circuits_on_missing_required_input():
    """A plan with a functional step calling read_doc without
    `filename` (the bug from the recipes.md report) must be rejected
    BEFORE the model is called — saves Haiku tokens AND gives the
    Planner a concrete retry diagnosis."""
    from wolfpaw.agents.plan_pre_evaluator import (
        PlanPreEvaluatorAgent, _validate_functional_step_inputs,
    )

    bad_plan = Plan(
        query="read my recipes file",
        summary="read recipes.md",
        steps=[
            Step(
                id="read_recipes", kind="functional",
                tool="read_doc",
                description="Read recipes.md",
                inputs={},  # ← the bug
            ),
        ],
        is_task=False, model_used="claude-sonnet-4-6", id=uuid4(),
    )
    diagnosis = _validate_functional_step_inputs(bad_plan)
    assert diagnosis is not None
    assert "filename" in diagnosis
    assert "read_doc" in diagnosis


async def test_evaluate_returns_rejection_verdict_without_calling_model(
    eval_env,
):
    """When the mechanical check fails the model must not be called.
    Verify by passing a FakeAnthropic with NO canned turns — if the
    model fires, FakeAnthropic raises IndexError."""
    fake_anthropic = FakeAnthropic([])  # no canned response
    agent = PlanPreEvaluatorAgent(
        model_client=ModelClient(anthropic=fake_anthropic),
    )
    bad_plan = Plan(
        query="x", summary="x",
        steps=[
            Step(
                id="s", kind="functional", tool="read_doc",
                description="read x", inputs={"filename": ""},
            ),
        ],
        is_task=False, model_used="claude-sonnet-4-6", id=uuid4(),
    )
    verdict = await agent.evaluate(
        ctx=ToolContext(user_id=uuid4()),
        content="x", plan=bad_plan,
    )
    assert verdict.approved is False
    assert verdict.achieves_objective is False
    assert "filename" in verdict.diagnosis
    # Model was not invoked.
    assert fake_anthropic.calls == []


async def test_evaluate_skips_unknown_tool_names():
    """Unknown tool names are user-tools or hallucinated — the
    mechanical check defers to the model (which sees the catalog and
    can flag a hallucination)."""
    from wolfpaw.agents.plan_pre_evaluator import _validate_functional_step_inputs

    plan = Plan(
        query="x", summary="x",
        steps=[
            Step(
                id="s", kind="functional",
                tool="possibly_a_user_tool",
                description="x",
                inputs={"some_arg": "yes"},
            ),
        ],
        is_task=False, model_used="claude-sonnet-4-6", id=uuid4(),
    )
    assert _validate_functional_step_inputs(plan) is None


async def test_evaluate_ignores_non_functional_steps():
    """Reasoning + evaluation + subagent steps don't need `tool` /
    `inputs` validation."""
    from wolfpaw.agents.plan_pre_evaluator import _validate_functional_step_inputs

    plan = Plan(
        query="x", summary="x",
        steps=[
            Step(id="r", kind="reasoning", description="think"),
            Step(id="e", kind="evaluation", description="check"),
        ],
        is_task=False, model_used="claude-sonnet-4-6", id=uuid4(),
    )
    assert _validate_functional_step_inputs(plan) is None


async def test_missing_tool_diagnosis_steers_toward_tool_creator():
    """The bug from the 'save recipe to DB' report: the model emitted
    a functional step with no tool, the diagnosis just said 'pick a
    tool', the retry came back in the same broken shape. The new
    diagnosis must explicitly mention `tool_creator` as the right
    answer for capability gaps."""
    from wolfpaw.agents.plan_pre_evaluator import _validate_functional_step_inputs

    plan = Plan(
        query="save the recipe to a SQL table",
        summary="x",
        steps=[
            Step(
                id="create_write_tool", kind="functional",
                description="Propose a new sql_write tool",
                # No tool, no inputs — the bug.
            ),
        ],
        is_task=False, model_used="claude-sonnet-4-6", id=uuid4(),
    )
    diagnosis = _validate_functional_step_inputs(plan)
    assert diagnosis is not None
    assert "tool_creator" in diagnosis
    assert "intent" in diagnosis
    assert "DO NOT" in diagnosis  # explicit don't-invent-tool warning


async def test_tool_creator_step_requires_inputs_intent():
    """tool_creator steps need `inputs.intent` — the validator should
    catch a tool_creator step without an intent before the Executor
    crashes on it."""
    from wolfpaw.agents.plan_pre_evaluator import _validate_functional_step_inputs

    plan = Plan(
        query="x", summary="x",
        steps=[
            Step(
                id="propose", kind="tool_creator",
                description="propose a tool",
                inputs={},  # missing intent
            ),
        ],
        is_task=False, model_used="claude-sonnet-4-6", id=uuid4(),
    )
    diagnosis = _validate_functional_step_inputs(plan)
    assert diagnosis is not None
    assert "tool_creator" in diagnosis
    assert "intent" in diagnosis


async def test_tool_creator_step_with_intent_passes_validation():
    from wolfpaw.agents.plan_pre_evaluator import _validate_functional_step_inputs

    plan = Plan(
        query="x", summary="x",
        steps=[
            Step(
                id="propose", kind="tool_creator",
                description="propose a tool",
                inputs={
                    "intent": "Execute INSERT/UPDATE/DELETE SQL.",
                    "required_inputs": ["statement"],
                },
            ),
        ],
        is_task=False, model_used="claude-sonnet-4-6", id=uuid4(),
    )
    assert _validate_functional_step_inputs(plan) is None


async def test_tool_creator_step_with_empty_intent_string_rejected():
    """Blank-string intent counts as missing — same posture as the
    `read_doc(filename="")` case for functional steps."""
    from wolfpaw.agents.plan_pre_evaluator import _validate_functional_step_inputs

    plan = Plan(
        query="x", summary="x",
        steps=[
            Step(
                id="p", kind="tool_creator", description="x",
                inputs={"intent": "   "},
            ),
        ],
        is_task=False, model_used="claude-sonnet-4-6", id=uuid4(),
    )
    diagnosis = _validate_functional_step_inputs(plan)
    assert diagnosis is not None
    assert "intent" in diagnosis


async def test_plan_with_pre_evaluation_ships_second_plan_unconditionally():
    """Spec: one retry max. The helper must NOT re-evaluate the
    second plan — it ships unconditionally even if rejection would
    have repeated."""
    p1 = _plan()
    p2 = Plan(
        query=p1.query, summary="still bad", steps=p1.steps,
        is_task=False, model_used=p1.model_used, id=uuid4(),
    )
    planner = _FakePlannerForHelper([p1, p2])
    pre = _FakePreEvaluator(verdicts=[
        PreEvalVerdict(
            approved=False, achieves_objective=False,
            simplifiable=False, better_than_past_plans=True,
            diagnosis="bad",
        ),
        # A second verdict would be returned IF the helper re-evaluated,
        # but it shouldn't. We assert evaluate() was only called once.
    ])
    plan, _verdict, retried = await plan_with_pre_evaluation(
        planner=planner, pre_evaluator=pre,
        ctx=ToolContext(user_id=uuid4()), thread_id=None,
        content="q", complexity_hint="moderate",
    )
    assert plan is p2
    assert retried is True
    assert len(pre.calls) == 1, "second plan must NOT be re-evaluated"
