"""Planner Agent tests — forced-tool_use parsing, retrieval-context
formatting, model tier selection, persistence wiring."""

from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Iterable
from uuid import UUID, uuid4

import pytest

from wolfpaw.agents.planner import PlannerAgent, _parse_plan_from_response
from wolfpaw.embeddings.base import EmbeddingClient, EmbeddingResult
from wolfpaw.metering.model_client import ModelClient
from wolfpaw.schemas import Plan, Step
from wolfpaw.toolbox.registry import ToolContext


@asynccontextmanager
async def _fake_acquire():
    yield None


class FakeEmbedder(EmbeddingClient):
    name = "fake"
    dimensions = 4

    def __init__(self):
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        self.calls.append(list(texts))
        return EmbeddingResult(
            vectors=[[0.1, 0.2, 0.3, 0.4] for _ in texts],
            input_tokens=len(texts) * 5,
            model=self.name,
        )


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


def _plan_tool_response(
    *,
    summary: str = "Do the thing.",
    steps: list[dict] | None = None,
    is_task: bool = False,
    skill: str | None = None,
    past_plan_id: str | None = None,
) -> SimpleNamespace:
    input_ = {"summary": summary, "is_task": is_task,
              "steps": steps or [
                  {"id": "s1", "kind": "reasoning", "description": "think"}
              ]}
    if skill is not None:
        input_["applied_skill_name"] = skill
    if past_plan_id is not None:
        input_["adapted_from_past_plan_id"] = past_plan_id
    return SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use", name="generate_plan",
                id="tu_plan", input=input_,
            ),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=50, output_tokens=80,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )


@pytest.fixture
def planner_env(monkeypatch):
    @dataclass
    class _Persisted:
        store_calls: list[dict] = field(default_factory=list)
        last_id: UUID | None = None

    persisted = _Persisted()

    async def fake_fetch_recent(_conn, *, thread_id, n=20):
        return []

    async def fake_fetch_summaries(_conn, *, thread_id):
        return []

    async def fake_search_user_messages(
        _conn, *, user_id, query_embedding, k=15,
        recency_weight=0.15, half_life_days=30.0, window=2,
        max_distance=2.0, exclude_recent_thread_id=None, exclude_recent_n=0,
    ):
        return []

    async def fake_search_similar(
        _conn, *, user_id, query_embedding, k=5, min_score=None,
    ):
        return []

    async def fake_search_skills(_conn, *, user_id, query_embedding, k=5):
        return []

    async def fake_list_user_tools(_conn, *, user_id):
        return []

    async def fake_store(
        _conn, *, user_id, thread_id, task_id, query, query_embedding,
        steps, final_answer=None, success=None, score=None,
        error=None, trace_id=None,
    ):
        new = uuid4()
        persisted.store_calls.append({
            "user_id": user_id, "query": query, "steps": steps,
            "thread_id": thread_id,
        })
        persisted.last_id = new
        return new

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
    monkeypatch.setattr("wolfpaw.agents.planner.acquire", _fake_acquire)
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.acquire", _fake_acquire,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.planner.conv.fetch_recent", fake_fetch_recent,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.planner.conv.fetch_summaries", fake_fetch_summaries,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.planner.conv.search_user_messages",
        fake_search_user_messages,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.planner.procedural.search_similar", fake_search_similar,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.planner.procedural.store", fake_store,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.planner.skills_mem.search_by_task", fake_search_skills,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.planner.user_tools_dao.list_approved_for_user",
        fake_list_user_tools,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.planner.bump_prompt_version", fake_bump,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.planner.record_usage", fake_record,
    )
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.record_usage", fake_record,
    )
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.get_active_price", fake_price,
    )
    yield persisted


# --- pure-unit parsing -----------------------------------------------------


def test_parse_plan_extracts_structured_fields():
    raw = _plan_tool_response(
        summary="X", steps=[
            {"id": "a", "kind": "functional", "tool": "web_search",
             "description": "search", "inputs": {"query": "q"},
             "parallel_group": 1},
        ],
        is_task=True, skill="research_one_pager",
    )
    plan = _parse_plan_from_response(raw, query="q", model="claude-sonnet-4-6")
    assert plan.summary == "X"
    assert plan.is_task is True
    assert plan.applied_skill_name == "research_one_pager"
    assert plan.model_used == "claude-sonnet-4-6"
    assert plan.steps[0].kind == "functional"
    assert plan.steps[0].tool == "web_search"
    assert plan.steps[0].parallel_group == 1


def test_parse_plan_falls_back_to_single_reasoning_step_when_no_tool_use():
    raw = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="I refuse")],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=5,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )
    plan = _parse_plan_from_response(raw, query="q", model="x")
    assert len(plan.steps) == 1
    assert plan.steps[0].kind == "reasoning"


def test_parse_plan_handles_bad_uuid_for_past_plan_id():
    raw = _plan_tool_response(past_plan_id="not-a-uuid")
    plan = _parse_plan_from_response(raw, query="q", model="x")
    assert plan.adapted_from_past_plan_id is None


def test_parse_plan_forces_is_task_when_plan_has_ask_user_step():
    """A plan with an `ask_user` step MUST route to the Task path — `ask_user`
    raises without a `task_id`. Even when the model emits is_task=False, the
    parser coerces it True. Regression for the "ask_user requires a Task
    context" inline-execution failure."""
    raw = _plan_tool_response(
        is_task=False,
        steps=[
            {"id": "ask", "kind": "functional", "tool": "ask_user",
             "description": "gather match details"},
            {"id": "ins", "kind": "functional", "tool": "sql_insert",
             "description": "log the match"},
        ],
    )
    plan = _parse_plan_from_response(raw, query="q", model="x")
    assert plan.is_task is True


def test_parse_plan_forces_is_task_for_tool_creator_step():
    """`tool_creator` uses `ask_user` internally, so it also needs a Task."""
    raw = _plan_tool_response(
        is_task=False,
        steps=[{"id": "tc", "kind": "tool_creator", "description": "make a tool"}],
    )
    plan = _parse_plan_from_response(raw, query="q", model="x")
    assert plan.is_task is True


def test_parse_plan_leaves_is_task_false_for_benign_plan():
    """No ask_user / tool_creator → respect the model's is_task=False."""
    raw = _plan_tool_response(
        is_task=False,
        steps=[
            {"id": "ins", "kind": "functional", "tool": "sql_insert",
             "description": "log the match"},
            {"id": "q", "kind": "functional", "tool": "sql_query",
             "description": "read it back"},
        ],
    )
    plan = _parse_plan_from_response(raw, query="q", model="x")
    assert plan.is_task is False


# --- integration with fakes ------------------------------------------------


async def test_plan_returns_structured_plan_and_persists(planner_env):
    fake_anthropic = FakeAnthropic([_plan_tool_response(summary="Hi.")])
    planner = PlannerAgent(
        model_client=ModelClient(anthropic=fake_anthropic),
        embedder=FakeEmbedder(),
    )
    plan, ctx = await planner.plan(
        ctx=ToolContext(user_id=uuid4()),
        thread_id=uuid4(),
        content="design a research one-pager",
    )
    assert isinstance(plan, Plan)
    assert plan.id is not None  # persisted, got an id back
    assert plan.summary == "Hi."
    assert len(plan.steps) >= 1
    assert len(planner_env.store_calls) == 1
    assert planner_env.store_calls[0]["query"] == "design a research one-pager"


async def test_plan_uses_sonnet_by_default(planner_env):
    fake_anthropic = FakeAnthropic([_plan_tool_response()])
    planner = PlannerAgent(
        model_client=ModelClient(anthropic=fake_anthropic),
        embedder=FakeEmbedder(),
    )
    await planner.plan(
        ctx=ToolContext(user_id=uuid4()), thread_id=uuid4(),
        content="x", complexity_hint="moderate",
    )
    assert fake_anthropic.calls[0]["model"] == "claude-sonnet-4-6"


async def test_plan_escalates_to_opus_for_ambitious(planner_env):
    fake_anthropic = FakeAnthropic([_plan_tool_response()])
    planner = PlannerAgent(
        model_client=ModelClient(anthropic=fake_anthropic),
        embedder=FakeEmbedder(),
    )
    await planner.plan(
        ctx=ToolContext(user_id=uuid4()), thread_id=uuid4(),
        content="x", complexity_hint="ambitious",
    )
    assert fake_anthropic.calls[0]["model"] == "claude-opus-4-7"


async def test_plan_forces_generate_plan_tool_use(planner_env):
    fake_anthropic = FakeAnthropic([_plan_tool_response()])
    planner = PlannerAgent(
        model_client=ModelClient(anthropic=fake_anthropic),
        embedder=FakeEmbedder(),
    )
    await planner.plan(
        ctx=ToolContext(user_id=uuid4()), thread_id=uuid4(),
        content="x",
    )
    call = fake_anthropic.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "generate_plan"}
    assert any(t["name"] == "generate_plan" for t in call["tools"])


async def test_plan_is_task_field_round_trips_from_tool_use(planner_env):
    """is_task is now a load-bearing routing decision — verify the
    Planner's forced-tool parser picks it up correctly from both
    true and false values."""
    for emitted in (True, False):
        fake_anthropic = FakeAnthropic([
            _plan_tool_response(is_task=emitted),
        ])
        planner = PlannerAgent(
            model_client=ModelClient(anthropic=fake_anthropic),
            embedder=FakeEmbedder(),
        )
        plan, _ctx = await planner.plan(
            ctx=ToolContext(user_id=uuid4()), thread_id=uuid4(),
            content="x",
        )
        assert plan.is_task is emitted


async def test_plan_includes_revision_diagnosis_in_system_prompt(planner_env):
    """When `revision_diagnosis` is set (Pre-Evaluator retry path),
    the planner prepends a revise-this preamble to the system prompt
    so the model treats it as the top-line instruction."""
    fake_anthropic = FakeAnthropic([_plan_tool_response(summary="revised")])
    planner = PlannerAgent(
        model_client=ModelClient(anthropic=fake_anthropic),
        embedder=FakeEmbedder(),
    )
    await planner.plan(
        ctx=ToolContext(user_id=uuid4()), thread_id=uuid4(),
        content="x",
        revision_diagnosis="drop step s3 — its output duplicates s2",
    )
    system = fake_anthropic.calls[0]["system"]
    assert "revising a rejected draft" in system
    assert "drop step s3" in system


async def test_plan_omits_revision_block_on_first_pass(planner_env):
    """No revision_diagnosis → no revise-this preamble in the prompt."""
    fake_anthropic = FakeAnthropic([_plan_tool_response()])
    planner = PlannerAgent(
        model_client=ModelClient(anthropic=fake_anthropic),
        embedder=FakeEmbedder(),
    )
    await planner.plan(
        ctx=ToolContext(user_id=uuid4()), thread_id=uuid4(), content="x",
    )
    system = fake_anthropic.calls[0]["system"]
    assert "revising a rejected draft" not in system


async def test_plan_inlines_retrieved_context_into_system_prompt(monkeypatch, planner_env):
    """When procedural + skills retrieval return matches, those rows
    should appear in the system prompt the planner sends."""
    from wolfpaw.memory import procedural, skills as skills_mem

    fake_past = [
        procedural.StoredPlan(
            id=uuid4(), user_id=uuid4(), thread_id=None, task_id=None,
            query="prior vendor research",
            steps=[{"id": "s1", "kind": "reasoning", "description": "think"}],
            score=90, similarity=0.92,
        )
    ]
    fake_skills = [
        skills_mem.Skill(
            id=uuid4(), user_id=None,
            name="vendor_comparison_spreadsheet",
            description="Research N vendors and emit a spreadsheet.",
            steps=[], similarity=0.88,
        )
    ]

    async def fake_search_similar(_conn, **_kw):
        return fake_past

    async def fake_search_skills(_conn, **_kw):
        return fake_skills

    monkeypatch.setattr(
        "wolfpaw.agents.planner.procedural.search_similar", fake_search_similar,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.planner.skills_mem.search_by_task", fake_search_skills,
    )

    fake_anthropic = FakeAnthropic([_plan_tool_response()])
    planner = PlannerAgent(
        model_client=ModelClient(anthropic=fake_anthropic),
        embedder=FakeEmbedder(),
    )
    await planner.plan(
        ctx=ToolContext(user_id=uuid4()), thread_id=uuid4(),
        content="research five vendors",
    )
    system = fake_anthropic.calls[0]["system"]
    assert "Past plans" in system
    assert "prior vendor research" in system
    assert "vendor_comparison_spreadsheet" in system
