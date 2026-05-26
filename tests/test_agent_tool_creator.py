"""Tool Creator unit tests — forced-tool parsing, spec normalization,
dedup short-circuit, approve / reject flow, feature-flag gating."""

from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Iterable
from uuid import UUID, uuid4

import pytest

from wolfpaw.agents.tool_creator import (
    ToolCreatorAgent,
    _normalize_spec,
)
from wolfpaw.embeddings.base import EmbeddingClient, EmbeddingResult
from wolfpaw.memory.tools import UserTool
from wolfpaw.metering.model_client import ModelClient
from wolfpaw.toolbox.registry import ToolContext, ToolError


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

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        return EmbeddingResult(
            vectors=[[0.1, 0.2, 0.3, 0.4] for _ in texts],
            input_tokens=len(texts) * 5,
            model=self.name,
        )


def _propose_response(
    *,
    name: str = "scrape_hn_frontpage",
    description: str = "Fetch the Hacker News front page and emit a list of {title, url, points} dicts.",
    schema: dict | None = None,
    implementation: str = "result = {'items': []}",
) -> SimpleNamespace:
    return SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use", name="propose_tool", id="tu_propose",
                input={
                    "name": name,
                    "description": description,
                    "input_schema": schema or {
                        "type": "object",
                        "properties": {
                            "limit": {"type": "integer", "default": 30},
                        },
                        "required": [],
                    },
                    "implementation": implementation,
                },
            ),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=80, output_tokens=120,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )


@pytest.fixture
def tool_creator_env(monkeypatch):
    async def fake_bump(_conn, *, agent, version_label, content_template):
        return SimpleNamespace(
            id=uuid4(), agent=agent, version_label=version_label,
            content_hash="fake", content_template=content_template,
        )

    async def fake_record(*args, **kwargs):
        return uuid4()

    async def fake_price(*args, **kwargs):
        return None

    async def fake_list_user_tools(_conn, *, user_id):
        return []

    monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
    monkeypatch.setattr(
        "wolfpaw.agents.tool_creator.acquire", _fake_acquire,
    )
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.acquire", _fake_acquire,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.tool_creator.bump_prompt_version", fake_bump,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.tool_creator.record_usage", fake_record,
    )
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.record_usage", fake_record,
    )
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.get_active_price", fake_price,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.tool_creator.tools_dao.list_approved_for_user",
        fake_list_user_tools,
    )
    # Reset the cached settings on each test so env-var overrides in
    # one test don't leak into the next via the lru_cache.
    from wolfpaw.config import get_settings

    monkeypatch.delenv("WOLFPAW_TOOL_CREATOR_ENABLED", raising=False)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    yield
    get_settings.cache_clear()  # type: ignore[attr-defined]


# --- _normalize_spec -------------------------------------------------------


def test_normalize_spec_accepts_well_formed_payload():
    raw = {
        "name": "good_tool_name",
        "description": "Does a thing.",
        "input_schema": {
            "type": "object", "properties": {"x": {"type": "integer"}},
        },
        "implementation": "result = {}",
    }
    spec = _normalize_spec(raw)
    assert spec is not None
    assert spec.name == "good_tool_name"


def test_normalize_spec_rejects_camel_case_names():
    raw = {
        "name": "BadName",
        "description": "x", "input_schema": {"type": "object", "properties": {}},
        "implementation": "result = {}",
    }
    assert _normalize_spec(raw) is None


def test_normalize_spec_rejects_dashes_and_spaces():
    for bad in ("kebab-name", "with space", "1leading_digit", ""):
        raw = {
            "name": bad, "description": "x",
            "input_schema": {"type": "object", "properties": {}},
            "implementation": "r = 1",
        }
        assert _normalize_spec(raw) is None, f"should reject {bad!r}"


def test_normalize_spec_rejects_missing_implementation():
    raw = {
        "name": "ok_name", "description": "x",
        "input_schema": {"type": "object", "properties": {}},
        "implementation": "",
    }
    assert _normalize_spec(raw) is None


def test_normalize_spec_rejects_bad_schema_shape():
    """Schema must be an object-typed JSON schema with `properties`."""
    raw = {
        "name": "ok_name", "description": "x",
        "input_schema": {"type": "array"},  # not an object schema
        "implementation": "result = {}",
    }
    assert _normalize_spec(raw) is None

    raw = {
        "name": "ok_name", "description": "x",
        "input_schema": {"type": "object"},  # no properties key
        "implementation": "result = {}",
    }
    assert _normalize_spec(raw) is None


# --- forced-tool parsing ---------------------------------------------------


async def test_create_tool_persists_and_approves_on_user_yes(
    monkeypatch, tool_creator_env,
):
    """Happy path: model proposes, no dedup hit, user approves, the
    row gets persisted as proposed and then promoted to approved."""
    fake_anthropic = FakeAnthropic([_propose_response()])
    stored_ids: list[UUID] = []
    approved: list[UUID] = []
    rejected: list[UUID] = []

    async def fake_store(_conn, **kw):
        new_id = uuid4()
        stored_ids.append(new_id)
        return new_id

    async def fake_approve(_conn, *, tool_id):
        approved.append(tool_id)
        return True

    async def fake_reject(_conn, *, tool_id):
        rejected.append(tool_id)
        return True

    async def fake_search(_conn, **_kw):
        return []  # no dedup hit

    monkeypatch.setattr(
        "wolfpaw.agents.tool_creator.tools_dao.store_proposed", fake_store,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.tool_creator.tools_dao.mark_approved", fake_approve,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.tool_creator.tools_dao.mark_rejected", fake_reject,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.tool_creator.tools_dao.search_by_task", fake_search,
    )

    # Fake ask_user: registry.get returns a stub that answers "approve".
    class _FakeAskTool:
        name = "ask_user"

        async def run(self, ctx, **inputs):
            return {"question_id": str(uuid4()), "answer": "approve"}

    class _FakeRegistry:
        def get(self, name):
            assert name == "ask_user"
            return _FakeAskTool()

        def names(self):
            return ["ask_user", "web_search", "http_get"]

    monkeypatch.setattr(
        "wolfpaw.agents.tool_creator.get_registry",
        lambda: _FakeRegistry(),
    )

    agent = ToolCreatorAgent(
        model_client=ModelClient(anthropic=fake_anthropic),
        embedder=FakeEmbedder(),
    )
    outcome = await agent.create_tool(
        ctx=ToolContext(user_id=uuid4(), task_id=uuid4()),
        intent="Fetch the HN front page.",
        required_inputs=["limit"],
    )

    assert outcome.status == "approved"
    assert outcome.name == "scrape_hn_frontpage"
    assert outcome.tool_id is not None
    assert len(stored_ids) == 1
    assert approved == [stored_ids[0]]
    assert rejected == []


async def test_create_tool_rejected_path_marks_rejected(
    monkeypatch, tool_creator_env,
):
    fake_anthropic = FakeAnthropic([_propose_response()])
    rejected: list[UUID] = []

    async def fake_store(_conn, **kw):
        return uuid4()

    async def fake_search(_conn, **_kw):
        return []

    async def fake_reject(_conn, *, tool_id):
        rejected.append(tool_id)
        return True

    async def fake_approve(_conn, *, tool_id):
        raise AssertionError("approve should NOT be called on reject path")

    monkeypatch.setattr(
        "wolfpaw.agents.tool_creator.tools_dao.store_proposed", fake_store,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.tool_creator.tools_dao.mark_approved", fake_approve,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.tool_creator.tools_dao.mark_rejected", fake_reject,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.tool_creator.tools_dao.search_by_task",
        fake_search,
    )

    class _FakeAskTool:
        name = "ask_user"

        async def run(self, ctx, **inputs):
            return {"question_id": str(uuid4()), "answer": "reject"}

    class _FakeRegistry:
        def get(self, name):
            return _FakeAskTool()

        def names(self):
            return []

    monkeypatch.setattr(
        "wolfpaw.agents.tool_creator.get_registry",
        lambda: _FakeRegistry(),
    )

    agent = ToolCreatorAgent(
        model_client=ModelClient(anthropic=fake_anthropic),
        embedder=FakeEmbedder(),
    )
    outcome = await agent.create_tool(
        ctx=ToolContext(user_id=uuid4(), task_id=uuid4()),
        intent="x",
    )
    assert outcome.status == "rejected"
    assert len(rejected) == 1


async def test_create_tool_short_circuits_on_dedup_hit(
    monkeypatch, tool_creator_env,
):
    """If an existing approved tool matches the embedding above
    threshold, we return ``status='duplicate'`` without calling the
    user or persisting a new row."""
    fake_anthropic = FakeAnthropic([_propose_response()])
    existing_id = uuid4()

    async def fake_search(_conn, *, user_id, query_embedding, k=5):
        return [
            UserTool(
                id=existing_id, user_id=user_id,
                name="scrape_hn_pages",
                description="Fetches HN; near-duplicate.",
                signature={"type": "object", "properties": {}},
                implementation="x",
                status="approved",
                similarity=0.99,
            ),
        ]

    async def fake_store(_conn, **_kw):
        raise AssertionError("dedup should short-circuit before persist")

    class _FakeRegistry:
        def get(self, name):
            raise AssertionError("ask_user should NOT be called on dedup hit")

        def names(self):
            return []

    monkeypatch.setattr(
        "wolfpaw.agents.tool_creator.tools_dao.search_by_task", fake_search,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.tool_creator.tools_dao.store_proposed", fake_store,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.tool_creator.get_registry",
        lambda: _FakeRegistry(),
    )

    agent = ToolCreatorAgent(
        model_client=ModelClient(anthropic=fake_anthropic),
        embedder=FakeEmbedder(),
    )
    outcome = await agent.create_tool(
        ctx=ToolContext(user_id=uuid4(), task_id=uuid4()),
        intent="Fetch HN.",
    )
    assert outcome.status == "duplicate"
    assert outcome.tool_id == existing_id
    assert outcome.name == "scrape_hn_pages"


async def test_create_tool_raises_when_task_context_missing(
    monkeypatch, tool_creator_env,
):
    fake_anthropic = FakeAnthropic([])
    agent = ToolCreatorAgent(
        model_client=ModelClient(anthropic=fake_anthropic),
        embedder=FakeEmbedder(),
    )
    with pytest.raises(ToolError, match="Task context"):
        await agent.create_tool(
            ctx=ToolContext(user_id=uuid4(), task_id=None),
            intent="x",
        )


async def test_create_tool_raises_when_disabled_via_config(
    monkeypatch, tool_creator_env,
):
    from wolfpaw.config import get_settings

    monkeypatch.setenv("WOLFPAW_TOOL_CREATOR_ENABLED", "false")
    get_settings.cache_clear()  # type: ignore[attr-defined]

    fake_anthropic = FakeAnthropic([])
    agent = ToolCreatorAgent(
        model_client=ModelClient(anthropic=fake_anthropic),
        embedder=FakeEmbedder(),
    )
    with pytest.raises(ToolError, match="disabled"):
        await agent.create_tool(
            ctx=ToolContext(user_id=uuid4(), task_id=uuid4()),
            intent="x",
        )


async def test_create_tool_raises_when_model_returns_no_tool_use(
    monkeypatch, tool_creator_env,
):
    no_tool = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="refused")],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=5,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )
    agent = ToolCreatorAgent(
        model_client=ModelClient(anthropic=FakeAnthropic([no_tool])),
        embedder=FakeEmbedder(),
    )
    with pytest.raises(ToolError, match="no tool_use"):
        await agent.create_tool(
            ctx=ToolContext(user_id=uuid4(), task_id=uuid4()),
            intent="x",
        )


async def test_create_tool_raises_when_proposed_spec_malformed(
    monkeypatch, tool_creator_env,
):
    """Sonnet hallucinated a bad name → no tool_use parsed → ToolError."""
    bad_response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use", name="propose_tool", id="tu",
                input={
                    "name": "BadCamelCase",
                    "description": "x",
                    "input_schema": {"type": "object", "properties": {}},
                    "implementation": "result = {}",
                },
            ),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=5,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )
    agent = ToolCreatorAgent(
        model_client=ModelClient(anthropic=FakeAnthropic([bad_response])),
        embedder=FakeEmbedder(),
    )
    with pytest.raises(ToolError, match="structured proposal"):
        await agent.create_tool(
            ctx=ToolContext(user_id=uuid4(), task_id=uuid4()),
            intent="x",
        )


async def test_create_tool_forces_propose_tool_tool_use(
    monkeypatch, tool_creator_env,
):
    fake_anthropic = FakeAnthropic([_propose_response()])
    captured: list[UUID] = []

    async def fake_store(_conn, **_kw):
        return uuid4()

    async def fake_approve(_conn, **_kw):
        return True

    async def fake_reject(_conn, **_kw):
        return True

    async def fake_search(_conn, **_kw):
        return []

    monkeypatch.setattr(
        "wolfpaw.agents.tool_creator.tools_dao.store_proposed", fake_store,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.tool_creator.tools_dao.mark_approved", fake_approve,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.tool_creator.tools_dao.mark_rejected", fake_reject,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.tool_creator.tools_dao.search_by_task", fake_search,
    )

    class _FakeAskTool:
        name = "ask_user"

        async def run(self, ctx, **inputs):
            return {"answer": "approve"}

    class _FakeRegistry:
        def get(self, name):
            return _FakeAskTool()

        def names(self):
            return []

    monkeypatch.setattr(
        "wolfpaw.agents.tool_creator.get_registry", lambda: _FakeRegistry(),
    )

    agent = ToolCreatorAgent(
        model_client=ModelClient(anthropic=fake_anthropic),
        embedder=FakeEmbedder(),
    )
    await agent.create_tool(
        ctx=ToolContext(user_id=uuid4(), task_id=uuid4()),
        intent="x",
    )
    call = fake_anthropic.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "propose_tool"}
    assert any(t["name"] == "propose_tool" for t in call["tools"])
