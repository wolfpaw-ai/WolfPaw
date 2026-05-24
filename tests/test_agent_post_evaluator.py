"""Post-Evaluator tests — forced tool_use parsing, score clamping,
fallback when the model bails."""

from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Iterable
from uuid import uuid4

import pytest

from wolfpaw.agents.post_evaluator import PostEvaluatorAgent
from wolfpaw.metering.model_client import ModelClient
from wolfpaw.schemas import (
    ExecutionPlan, Plan, PostEvalVerdict, Step, StepResult, StepStatus,
)
from wolfpaw.toolbox.registry import ToolContext


@asynccontextmanager
async def _fake_acquire():
    yield None


class FakeAnthropic:
    def __init__(self, turns: Iterable[Any]) -> None:
        self._turns = list(turns)
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    async def create(self, **kwargs):
        snapshot = dict(kwargs)
        if "messages" in snapshot:
            snapshot["messages"] = copy.deepcopy(snapshot["messages"])
        self.calls.append(snapshot)
        return self._turns.pop(0)


def _tool_use_response(*, score: int, summary: str = "ok",
                       went_well: str = "", went_wrong: str = "",
                       improvements: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use", name="record_score", id="tu_score",
                input={
                    "score": score, "summary": summary,
                    "what_went_well": went_well,
                    "what_went_wrong": went_wrong,
                    "improvements": improvements,
                },
            ),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=20, output_tokens=8,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )


def _plain_text_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=4,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )


def _plan() -> Plan:
    return Plan(
        query="research X",
        summary="Find sources, summarize.",
        steps=[
            Step(id="s1", kind="functional", tool="web_search",
                 description="search"),
            Step(id="s2", kind="reasoning", description="summarize"),
        ],
        id=uuid4(), model_used="claude-sonnet-4-6",
    )


def _execution(plan: Plan, *, success: bool = True,
               final_answer: str = "Here's the answer.") -> ExecutionPlan:
    return ExecutionPlan(
        plan=plan,
        results=[
            StepResult(step_id=s.id, kind=s.kind,
                       status=StepStatus.COMPLETED if success else StepStatus.FAILED,
                       output={"ok": True} if s.kind == "functional" else "summary text",
                       error=None if success else "boom")
            for s in plan.steps
        ],
        final_answer=final_answer,
        success=success,
        error=None if success else "boom",
    )


@pytest.fixture
def eval_env(monkeypatch):
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
        "wolfpaw.agents.post_evaluator.acquire", _fake_acquire,
    )
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.acquire", _fake_acquire,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.post_evaluator.bump_prompt_version", fake_bump,
    )
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.record_usage", fake_record,
    )
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.get_active_price", fake_price,
    )


# --- tests -----------------------------------------------------------------


async def test_evaluate_returns_verdict_from_forced_tool_use(eval_env):
    fake = FakeAnthropic([_tool_use_response(
        score=88, summary="strong",
        went_well="clean tool calls", went_wrong="",
        improvements="cache the search next time",
    )])
    evaluator = PostEvaluatorAgent(model_client=ModelClient(anthropic=fake))
    plan = _plan()
    verdict = await evaluator.evaluate(
        ctx=ToolContext(user_id=uuid4()),
        plan=plan, execution=_execution(plan),
    )
    assert verdict == PostEvalVerdict(
        score=88, summary="strong",
        what_went_well="clean tool calls",
        what_went_wrong="",
        improvements="cache the search next time",
    )


async def test_evaluate_forces_record_score_tool(eval_env):
    fake = FakeAnthropic([_tool_use_response(score=70)])
    evaluator = PostEvaluatorAgent(model_client=ModelClient(anthropic=fake))
    plan = _plan()
    await evaluator.evaluate(
        ctx=ToolContext(user_id=uuid4()),
        plan=plan, execution=_execution(plan),
    )
    call = fake.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "record_score"}
    assert any(t["name"] == "record_score" for t in call["tools"])


async def test_evaluate_clamps_out_of_range_scores(eval_env):
    fake = FakeAnthropic([_tool_use_response(score=150)])
    evaluator = PostEvaluatorAgent(model_client=ModelClient(anthropic=fake))
    plan = _plan()
    verdict = await evaluator.evaluate(
        ctx=ToolContext(user_id=uuid4()),
        plan=plan, execution=_execution(plan),
    )
    assert verdict.score == 100


async def test_evaluate_clamps_negative_score(eval_env):
    fake = FakeAnthropic([_tool_use_response(score=-10)])
    evaluator = PostEvaluatorAgent(model_client=ModelClient(anthropic=fake))
    plan = _plan()
    verdict = await evaluator.evaluate(
        ctx=ToolContext(user_id=uuid4()),
        plan=plan, execution=_execution(plan),
    )
    assert verdict.score == 0


async def test_evaluate_fallback_when_no_tool_use(eval_env):
    """If the model bails on the forced tool_use, default to a neutral
    50 on success and 0 on failure so the Router still has something
    to persist."""
    fake = FakeAnthropic([_plain_text_response("I refuse the tool.")])
    evaluator = PostEvaluatorAgent(model_client=ModelClient(anthropic=fake))
    plan = _plan()
    verdict = await evaluator.evaluate(
        ctx=ToolContext(user_id=uuid4()),
        plan=plan, execution=_execution(plan, success=True),
    )
    assert verdict.score == 50
    assert "defaulted" in verdict.summary


async def test_evaluate_fallback_score_zero_on_failed_execution(eval_env):
    fake = FakeAnthropic([_plain_text_response("nope.")])
    evaluator = PostEvaluatorAgent(model_client=ModelClient(anthropic=fake))
    plan = _plan()
    verdict = await evaluator.evaluate(
        ctx=ToolContext(user_id=uuid4()),
        plan=plan, execution=_execution(plan, success=False),
    )
    assert verdict.score == 0


async def test_evaluate_prompt_includes_user_request_and_final_answer(eval_env):
    fake = FakeAnthropic([_tool_use_response(score=80)])
    evaluator = PostEvaluatorAgent(model_client=ModelClient(anthropic=fake))
    plan = _plan()
    await evaluator.evaluate(
        ctx=ToolContext(user_id=uuid4()),
        plan=plan, execution=_execution(plan, final_answer="HERE IS THE ANSWER"),
    )
    user_msg = fake.calls[0]["messages"][0]["content"]
    assert plan.query in user_msg
    assert plan.summary in user_msg
    assert "HERE IS THE ANSWER" in user_msg
