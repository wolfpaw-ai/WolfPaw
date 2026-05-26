"""TriageAgent tests — forced-tool_use classification, fallback path, and
the right Anthropic call shape (system prompt, tool_choice).

The Anthropic client + DB are both faked, same posture as the Quick Agent
tests."""

from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Iterable
from uuid import UUID, uuid4

import pytest

from wolfpaw.agents.triage import TriageAgent, TriageVerdict
from wolfpaw.metering.model_client import ModelClient
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


def _tool_use_response(
    *, route: str, complexity: str = "simple", reasoning: str = "test",
) -> SimpleNamespace:
    return SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                name="classify",
                id="tu_classify",
                input={
                    "route": route, "complexity": complexity,
                    "reasoning": reasoning,
                },
            ),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=12, output_tokens=4,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )


def _plain_text_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=12, output_tokens=4,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )


@pytest.fixture
def triage_env(monkeypatch):
    async def fake_fetch_recent(_conn, *, thread_id, n=20):
        return []

    async def fake_fetch_summaries(_conn, *, thread_id):
        return []

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
    monkeypatch.setattr("wolfpaw.agents.triage.acquire", _fake_acquire)
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.acquire", _fake_acquire,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.triage.conv.fetch_recent", fake_fetch_recent,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.triage.conv.fetch_summaries", fake_fetch_summaries,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.triage.bump_prompt_version", fake_bump,
    )
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.record_usage", fake_record,
    )
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.get_active_price", fake_price,
    )


# --- tests -----------------------------------------------------------------


async def test_returns_verdict_from_forced_tool_use(triage_env):
    fake = FakeAnthropic([
        _tool_use_response(
            route="plan", complexity="moderate",
            reasoning="multi-step research request",
        ),
    ])
    agent = TriageAgent(model_client=ModelClient(anthropic=fake))
    ctx = ToolContext(user_id=uuid4())
    verdict = await agent.classify(
        ctx=ctx, thread_id=uuid4(),
        content="research five vendors and put them in a spreadsheet",
    )
    assert verdict == TriageVerdict(
        route="plan", complexity="moderate",
        reasoning="multi-step research request",
    )


async def test_forces_classify_tool(triage_env):
    fake = FakeAnthropic([_tool_use_response(route="quick")])
    agent = TriageAgent(model_client=ModelClient(anthropic=fake))
    await agent.classify(
        ctx=ToolContext(user_id=uuid4()),
        thread_id=uuid4(), content="hello",
    )
    # tool_choice must force the model to use 'classify'.
    call = fake.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "classify"}
    assert any(t["name"] == "classify" for t in call["tools"])


async def test_includes_recent_history_plus_new_user_turn(triage_env, monkeypatch):
    history = [
        SimpleNamespace(role="user", content="prior message"),
        SimpleNamespace(role="assistant", content="prior reply"),
    ]

    async def fake_recent(_conn, *, thread_id, n=20):
        return history

    monkeypatch.setattr(
        "wolfpaw.agents.triage.conv.fetch_recent", fake_recent,
    )
    fake = FakeAnthropic([_tool_use_response(route="quick")])
    agent = TriageAgent(model_client=ModelClient(anthropic=fake))
    await agent.classify(
        ctx=ToolContext(user_id=uuid4()),
        thread_id=uuid4(), content="newest message",
    )
    msgs = fake.calls[0]["messages"]
    assert [(m["role"], m["content"]) for m in msgs] == [
        ("user", "prior message"),
        ("assistant", "prior reply"),
        ("user", "newest message"),
    ]


async def test_unknown_route_normalizes_to_quick(triage_env):
    fake = FakeAnthropic([_tool_use_response(route="bogus")])
    agent = TriageAgent(model_client=ModelClient(anthropic=fake))
    verdict = await agent.classify(
        ctx=ToolContext(user_id=uuid4()),
        thread_id=uuid4(), content="x",
    )
    assert verdict.route == "quick"


async def test_no_tool_use_block_falls_back_to_quick(triage_env):
    fake = FakeAnthropic([_plain_text_response("I refuse the tool")])
    agent = TriageAgent(model_client=ModelClient(anthropic=fake))
    verdict = await agent.classify(
        ctx=ToolContext(user_id=uuid4()),
        thread_id=uuid4(), content="x",
    )
    assert verdict.route == "quick"
    assert "defaulted" in verdict.reasoning
