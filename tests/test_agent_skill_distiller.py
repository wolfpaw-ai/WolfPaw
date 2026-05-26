"""Skill Distiller unit tests — forced-tool parsing, the
`maybe_distill_skill` flow (threshold gate, reusability heuristic,
dedup, distill, persist), failure posture."""

from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Iterable
from uuid import UUID, uuid4

import pytest

from wolfpaw.agents.skill_distiller import (
    SkillDistillerAgent,
    _is_reusable,
    _normalize_distilled,
    maybe_distill_skill,
)
from wolfpaw.embeddings.base import EmbeddingClient, EmbeddingResult
from wolfpaw.memory import skills as skills_mem
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
        self.calls: list[dict] = []
        self.messages = self

    async def create(self, **kwargs):
        snapshot = dict(kwargs)
        if "messages" in snapshot:
            snapshot["messages"] = copy.deepcopy(snapshot["messages"])
        self.calls.append(snapshot)
        return self._turns.pop(0)


class FakeEmbedder(EmbeddingClient):
    name = "fake"
    dimensions = 4

    def __init__(self):
        self.calls: list[str] = []

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        self.calls.extend(texts)
        return EmbeddingResult(
            vectors=[[0.1, 0.2, 0.3, 0.4] for _ in texts],
            input_tokens=len(texts) * 5,
            model=self.name,
        )


def _emit_skill_response(
    *,
    name: str = "vendor_lookup_table",
    description: str = "Research N vendors and emit a comparison table.",
    ingredients: dict | None = None,
    generalized_steps: list[dict] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use", name="emit_skill", id="tu_skill",
                input={
                    "name": name,
                    "description": description,
                    "ingredients": ingredients or {
                        "tools": ["web_search", "create_spreadsheet"],
                    },
                    "generalized_steps": generalized_steps or [
                        {"id": "search", "kind": "functional",
                         "tool": "web_search",
                         "description": "Find each vendor's homepage."},
                        {"id": "emit", "kind": "functional",
                         "tool": "create_spreadsheet",
                         "description": "Emit a .xlsx comparison."},
                    ],
                },
            ),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=50, output_tokens=120,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )


def _multi_step_plan(*, plan_id: UUID | None = None) -> Plan:
    return Plan(
        query="research five vendors and compare them",
        summary="multi-step plan with tools",
        steps=[
            Step(id="s1", kind="functional",
                 description="find vendors", tool="web_search"),
            Step(id="s2", kind="reasoning",
                 description="summarize"),
            Step(id="s3", kind="functional",
                 description="emit spreadsheet", tool="create_spreadsheet"),
        ],
        is_task=False, model_used="claude-sonnet-4-6",
        id=plan_id or uuid4(),
    )


def _execution_for(plan: Plan) -> ExecutionPlan:
    return ExecutionPlan(
        plan=plan,
        results=[
            StepResult(
                step_id=s.id, kind=s.kind,
                status=StepStatus.COMPLETED, output="ok",
            )
            for s in plan.steps
        ],
        final_answer="here's your table", success=True,
    )


def _verdict(score: int = 95) -> PostEvalVerdict:
    return PostEvalVerdict(
        score=score, summary="solid",
        what_went_well="clean tool use",
        what_went_wrong="",
        improvements="",
    )


@pytest.fixture
def distiller_env(monkeypatch):
    """Stub the DB hooks ModelClient + SkillDistillerAgent reach."""
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
        "wolfpaw.agents.skill_distiller.acquire", _fake_acquire,
    )
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.acquire", _fake_acquire,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.skill_distiller.bump_prompt_version", fake_bump,
    )
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.record_usage", fake_record,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.skill_distiller.record_usage", fake_record,
    )
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.get_active_price", fake_price,
    )


# --- pure helpers ----------------------------------------------------------


def test_is_reusable_rejects_single_step_plans():
    plan = Plan(
        query="q", summary="s",
        steps=[Step(id="only", kind="reasoning", description="think")],
        is_task=False, model_used="claude-sonnet-4-6", id=uuid4(),
    )
    assert _is_reusable(plan) is False


def test_is_reusable_rejects_multi_step_reasoning_only_plans():
    """Multi-step but all reasoning — no tools used means not a real
    procedure, just a chain of thoughts."""
    plan = Plan(
        query="q", summary="s",
        steps=[
            Step(id="a", kind="reasoning", description="think a"),
            Step(id="b", kind="reasoning", description="think b"),
            Step(id="c", kind="reasoning", description="conclude"),
        ],
        is_task=False, model_used="claude-sonnet-4-6", id=uuid4(),
    )
    assert _is_reusable(plan) is False


def test_is_reusable_accepts_multi_step_with_a_functional():
    plan = _multi_step_plan()
    assert _is_reusable(plan) is True


def test_is_reusable_accepts_subagent_steps():
    plan = Plan(
        query="q", summary="s",
        steps=[
            Step(id="a", kind="subagent",
                 description="delegate research",
                 inputs={"query": "..."}),
            Step(id="b", kind="reasoning", description="synthesize"),
        ],
        is_task=True, model_used="claude-sonnet-4-6", id=uuid4(),
    )
    assert _is_reusable(plan) is True


def test_normalize_distilled_drops_payloads_with_missing_fields():
    assert _normalize_distilled({"name": "x"}) is None
    assert _normalize_distilled(
        {"name": "x", "description": "y",
         "ingredients": {}, "generalized_steps": []}
    ) is None
    assert _normalize_distilled(
        {"name": "", "description": "y",
         "ingredients": {}, "generalized_steps": [{"id": "a", "kind": "reasoning"}]}
    ) is None


def test_normalize_distilled_coerces_non_dict_ingredients_to_empty():
    """If the model returns ingredients as a list or string, we fall
    back to {} rather than erroring."""
    raw = _normalize_distilled({
        "name": "x", "description": "y",
        "ingredients": "not a dict",
        "generalized_steps": [{"id": "s1", "kind": "reasoning",
                               "description": "t"}],
    })
    assert raw is not None
    assert raw.ingredients == {}


# --- agent forced-tool parsing --------------------------------------------


async def test_distill_returns_normalized_skill_on_forced_tool_use(distiller_env):
    fake_anthropic = FakeAnthropic([_emit_skill_response()])
    agent = SkillDistillerAgent(
        model_client=ModelClient(anthropic=fake_anthropic),
    )
    plan = _multi_step_plan()
    result = await agent.distill(
        ctx=ToolContext(user_id=uuid4()),
        plan=plan,
        execution=_execution_for(plan),
        verdict=_verdict(),
    )
    assert result is not None
    assert result.name == "vendor_lookup_table"
    assert "comparison table" in result.description
    assert result.ingredients["tools"] == ["web_search", "create_spreadsheet"]
    assert len(result.generalized_steps) == 2


async def test_distill_forces_emit_skill_tool(distiller_env):
    fake_anthropic = FakeAnthropic([_emit_skill_response()])
    agent = SkillDistillerAgent(
        model_client=ModelClient(anthropic=fake_anthropic),
    )
    plan = _multi_step_plan()
    await agent.distill(
        ctx=ToolContext(user_id=uuid4()),
        plan=plan, execution=_execution_for(plan), verdict=_verdict(),
    )
    call = fake_anthropic.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "emit_skill"}
    assert any(t["name"] == "emit_skill" for t in call["tools"])


async def test_distill_returns_none_when_no_tool_use_block(distiller_env):
    no_tool = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="refused")],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=5,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )
    fake_anthropic = FakeAnthropic([no_tool])
    agent = SkillDistillerAgent(
        model_client=ModelClient(anthropic=fake_anthropic),
    )
    plan = _multi_step_plan()
    result = await agent.distill(
        ctx=ToolContext(user_id=uuid4()),
        plan=plan, execution=_execution_for(plan), verdict=_verdict(),
    )
    assert result is None


# --- maybe_distill_skill flow ---------------------------------------------


@dataclass
class _FakeDistillAgent:
    response: Any
    calls: list[dict] = field(default_factory=list)

    async def distill(self, *, ctx, plan, execution, verdict):
        self.calls.append({"plan_id": plan.id, "score": verdict.score})
        return self.response


async def test_maybe_distill_skips_when_score_below_threshold(
    monkeypatch, distiller_env,
):
    """Score 89 < default threshold 90 → no distill call, no embed."""
    embedder = FakeEmbedder()
    distiller = _FakeDistillAgent(response=None)
    skill = await maybe_distill_skill(
        ctx=ToolContext(user_id=uuid4()),
        plan=_multi_step_plan(),
        execution=_execution_for(_multi_step_plan()),
        verdict=_verdict(score=89),
        distiller=distiller, embedder=embedder,
    )
    assert skill is None
    assert distiller.calls == []
    assert embedder.calls == []


async def test_maybe_distill_skips_when_plan_not_reusable(
    monkeypatch, distiller_env,
):
    """Single-step plan even with score 95 doesn't emit."""
    plan = Plan(
        query="q", summary="s",
        steps=[Step(id="only", kind="reasoning", description="t")],
        is_task=False, model_used="claude-sonnet-4-6", id=uuid4(),
    )
    embedder = FakeEmbedder()
    distiller = _FakeDistillAgent(response=None)
    skill = await maybe_distill_skill(
        ctx=ToolContext(user_id=uuid4()),
        plan=plan, execution=_execution_for(plan), verdict=_verdict(95),
        distiller=distiller, embedder=embedder,
    )
    assert skill is None
    assert distiller.calls == []


async def test_maybe_distill_skips_when_plan_id_missing(
    monkeypatch, distiller_env,
):
    """Without a persisted plan row, there's no source_plan_id to point
    at — skip rather than emit a dangling skill."""
    plan = _multi_step_plan()
    plan_no_id = Plan(
        query=plan.query, summary=plan.summary, steps=plan.steps,
        is_task=plan.is_task, model_used=plan.model_used, id=None,
    )
    embedder = FakeEmbedder()
    distiller = _FakeDistillAgent(response=None)
    skill = await maybe_distill_skill(
        ctx=ToolContext(user_id=uuid4()),
        plan=plan_no_id, execution=_execution_for(plan_no_id),
        verdict=_verdict(95),
        distiller=distiller, embedder=embedder,
    )
    assert skill is None


async def test_maybe_distill_dedup_hit_skips_distill_call(
    monkeypatch, distiller_env,
):
    """When search_by_task returns a skill with similarity ≥ threshold,
    we skip emission AND the (expensive) distill call."""
    embedder = FakeEmbedder()
    distiller = _FakeDistillAgent(response=None)

    async def fake_search(_conn, *, user_id, query_embedding, k=5):
        return [
            skills_mem.Skill(
                id=uuid4(), user_id=None,
                name="vendor_comparison_spreadsheet",
                description="already exists",
                similarity=0.95,
            ),
        ]

    monkeypatch.setattr(
        "wolfpaw.agents.skill_distiller.skills_mem.search_by_task",
        fake_search,
    )

    skill = await maybe_distill_skill(
        ctx=ToolContext(user_id=uuid4()),
        plan=_multi_step_plan(),
        execution=_execution_for(_multi_step_plan()),
        verdict=_verdict(95),
        distiller=distiller, embedder=embedder,
    )
    assert skill is None
    assert distiller.calls == []


async def test_maybe_distill_emits_skill_and_persists(monkeypatch, distiller_env):
    """Happy path: threshold ok, reusable, no dedup hit → distill +
    persist. The returned Skill points at the originating plan."""
    embedder = FakeEmbedder()
    plan = _multi_step_plan()
    user_id = uuid4()
    raw_response = SimpleNamespace(
        # the inner distill agent will be a fake — this is just for
        # symmetry. We use a _FakeDistillAgent.
    )

    from wolfpaw.agents.skill_distiller import _RawSkill

    fake_distill = _FakeDistillAgent(
        response=_RawSkill(
            name="vendor_lookup_table",
            description="Research N vendors and emit a comparison table.",
            ingredients={"tools": ["web_search", "create_spreadsheet"]},
            generalized_steps=[
                {"id": "search", "kind": "functional",
                 "tool": "web_search", "description": "find homepages"},
                {"id": "emit", "kind": "functional",
                 "tool": "create_spreadsheet",
                 "description": "emit .xlsx"},
            ],
        ),
    )

    async def fake_search(_conn, *, user_id, query_embedding, k=5):
        return []  # no near duplicates

    persisted: list[dict] = []

    async def fake_store_emitted(_conn, **kw):
        persisted.append(kw)
        return uuid4()

    monkeypatch.setattr(
        "wolfpaw.agents.skill_distiller.skills_mem.search_by_task",
        fake_search,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.skill_distiller.skills_mem.store_emitted",
        fake_store_emitted,
    )

    skill = await maybe_distill_skill(
        ctx=ToolContext(user_id=user_id),
        plan=plan, execution=_execution_for(plan), verdict=_verdict(95),
        distiller=fake_distill, embedder=embedder,
    )
    assert skill is not None
    assert skill.name == "vendor_lookup_table"
    assert skill.user_id == user_id
    assert skill.source_plan_id == plan.id
    assert skill.score == 95
    assert len(fake_distill.calls) == 1
    assert len(persisted) == 1
    assert persisted[0]["name"] == "vendor_lookup_table"
    assert persisted[0]["source_plan_id"] == plan.id
    # Embedder was called twice: once for the dedup query, once for
    # the canonical skill-description embedding stored in the row.
    assert len(embedder.calls) == 2


async def test_maybe_distill_emits_sse_event_on_success(
    monkeypatch, distiller_env,
):
    embedder = FakeEmbedder()
    plan = _multi_step_plan()

    from wolfpaw.agents.skill_distiller import _RawSkill

    fake_distill = _FakeDistillAgent(
        response=_RawSkill(
            name="emitted_skill",
            description="test description",
            ingredients={"tools": ["x"]},
            generalized_steps=[
                {"id": "a", "kind": "functional", "tool": "x",
                 "description": "do x"},
                {"id": "b", "kind": "reasoning", "description": "think"},
            ],
        ),
    )

    async def fake_search(_conn, **_kw):
        return []

    async def fake_store(_conn, **_kw):
        return uuid4()

    monkeypatch.setattr(
        "wolfpaw.agents.skill_distiller.skills_mem.search_by_task",
        fake_search,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.skill_distiller.skills_mem.store_emitted",
        fake_store,
    )

    emitted: list[tuple[str, str]] = []

    async def emit(event, data):
        emitted.append((event, data))

    await maybe_distill_skill(
        ctx=ToolContext(user_id=uuid4()),
        plan=plan, execution=_execution_for(plan), verdict=_verdict(95),
        distiller=fake_distill, embedder=embedder, emit=emit,
    )
    skill_events = [e for e in emitted if e[0] == "skill_emitted"]
    assert skill_events == [("skill_emitted", "emitted_skill")]


async def test_maybe_distill_emit_not_called_when_skipped(
    monkeypatch, distiller_env,
):
    """If we skip emission (below threshold), no SSE event fires."""
    embedder = FakeEmbedder()
    plan = _multi_step_plan()
    emitted: list = []

    async def emit(event, data):
        emitted.append((event, data))

    await maybe_distill_skill(
        ctx=ToolContext(user_id=uuid4()),
        plan=plan, execution=_execution_for(plan), verdict=_verdict(70),
        distiller=_FakeDistillAgent(response=None),
        embedder=embedder, emit=emit,
    )
    assert emitted == []


async def test_maybe_distill_returns_none_when_distiller_fails(
    monkeypatch, distiller_env,
):
    """A model exception inside distill() is logged + swallowed; the
    helper returns None rather than propagating."""
    embedder = FakeEmbedder()
    plan = _multi_step_plan()

    class _Broken:
        async def distill(self, **_kw):
            raise RuntimeError("anthropic down")

    async def fake_search(_conn, **_kw):
        return []

    monkeypatch.setattr(
        "wolfpaw.agents.skill_distiller.skills_mem.search_by_task",
        fake_search,
    )

    skill = await maybe_distill_skill(
        ctx=ToolContext(user_id=uuid4()),
        plan=plan, execution=_execution_for(plan), verdict=_verdict(95),
        distiller=_Broken(), embedder=embedder,
    )
    assert skill is None


async def test_maybe_distill_returns_none_when_persist_fails(
    monkeypatch, distiller_env,
):
    embedder = FakeEmbedder()
    plan = _multi_step_plan()

    from wolfpaw.agents.skill_distiller import _RawSkill

    fake_distill = _FakeDistillAgent(
        response=_RawSkill(
            name="x", description="y",
            ingredients={}, generalized_steps=[
                {"id": "a", "kind": "reasoning", "description": "t"},
            ],
        ),
    )

    async def fake_search(_conn, **_kw):
        return []

    async def broken_store(_conn, **_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "wolfpaw.agents.skill_distiller.skills_mem.search_by_task",
        fake_search,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.skill_distiller.skills_mem.store_emitted",
        broken_store,
    )

    skill = await maybe_distill_skill(
        ctx=ToolContext(user_id=uuid4()),
        plan=plan, execution=_execution_for(plan), verdict=_verdict(95),
        distiller=fake_distill, embedder=embedder,
    )
    assert skill is None


async def test_maybe_distill_threshold_tunable_via_setting(
    monkeypatch, distiller_env,
):
    """Lowering WOLFPAW_SKILL_EMIT_MIN_SCORE makes lower-scored plans
    eligible — verifies the threshold isn't hardcoded."""
    from wolfpaw.config import get_settings

    monkeypatch.setenv("WOLFPAW_SKILL_EMIT_MIN_SCORE", "70")
    get_settings.cache_clear()  # type: ignore[attr-defined]

    embedder = FakeEmbedder()
    plan = _multi_step_plan()
    from wolfpaw.agents.skill_distiller import _RawSkill

    fake_distill = _FakeDistillAgent(
        response=_RawSkill(
            name="x", description="y",
            ingredients={}, generalized_steps=[
                {"id": "a", "kind": "functional", "tool": "web_search",
                 "description": "t"},
                {"id": "b", "kind": "reasoning", "description": "u"},
            ],
        ),
    )

    async def fake_search(_conn, **_kw):
        return []

    async def fake_store(_conn, **_kw):
        return uuid4()

    monkeypatch.setattr(
        "wolfpaw.agents.skill_distiller.skills_mem.search_by_task",
        fake_search,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.skill_distiller.skills_mem.store_emitted",
        fake_store,
    )

    skill = await maybe_distill_skill(
        ctx=ToolContext(user_id=uuid4()),
        plan=plan, execution=_execution_for(plan), verdict=_verdict(75),
        distiller=fake_distill, embedder=embedder,
    )
    assert skill is not None
    assert len(fake_distill.calls) == 1
