"""Quick Agent tests — tool loop, persistence, iteration cap.

The Anthropic client and the DB layer are both faked so tests run fast
and don't need Postgres. The tool loop logic is the high-value bit; the
ModelClient + recorder integration is covered by its own tests.
"""

from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Iterable
from uuid import UUID, uuid4

import pytest

from wolfpaw.agents.quick import QuickAgent
from wolfpaw.metering.model_client import ModelClient
from wolfpaw.metering.types import ModelCallResult, TokenCounts
from wolfpaw.toolbox.registry import ToolContext

# --- fakes -----------------------------------------------------------------


@dataclass
class FakeTurn:
    """One canned Anthropic response: a list of content blocks + stop_reason."""

    content: list[Any]
    stop_reason: str = "end_turn"


class FakeAnthropic:
    """Mimics enough of `anthropic.AsyncAnthropic` for the agent loop."""

    def __init__(self, turns: Iterable[FakeTurn]) -> None:
        self._turns = list(turns)
        self.calls: list[dict[str, Any]] = []
        self.messages = self  # so .messages.create resolves to us

    async def create(self, **kwargs):
        # Snapshot kwargs at call time — the agent mutates `messages` in
        # place after we return, so storing the reference would let later
        # writes leak into our captured history.
        snapshot = dict(kwargs)
        if "messages" in snapshot:
            snapshot["messages"] = copy.deepcopy(snapshot["messages"])
        self.calls.append(snapshot)
        if not self._turns:
            raise AssertionError("FakeAnthropic ran out of canned turns")
        turn = self._turns.pop(0)
        return SimpleNamespace(
            content=turn.content,
            stop_reason=turn.stop_reason,
            usage=SimpleNamespace(
                input_tokens=10, output_tokens=5,
                cache_read_input_tokens=0, cache_creation_input_tokens=0,
            ),
        )


def _text(s: str) -> Any:
    return SimpleNamespace(type="text", text=s)


def _tool_use(name: str, input_: dict, id_: str = "tu_1") -> Any:
    return SimpleNamespace(type="tool_use", name=name, input=input_, id=id_)


@asynccontextmanager
async def _fake_acquire():
    yield None


@dataclass
class _FakeStore:
    """In-memory stand-in for the messages table that agent.handle hits."""

    threads: dict[UUID, list[dict]] = field(default_factory=dict)
    prompt_version_seeded: bool = False


@pytest.fixture
def agent_env(monkeypatch):
    store = _FakeStore()

    async def fake_get_or_create_thread(_conn, *, user_id, channel, thread_id=None):
        if thread_id is None or thread_id not in store.threads:
            tid = uuid4()
            store.threads[tid] = []
            return tid
        return thread_id

    async def fake_fetch_recent(_conn, *, thread_id, n=20):
        msgs = store.threads.get(thread_id, [])[-n:]
        return [
            SimpleNamespace(
                role=m["role"], content=m["content"], id=uuid4(),
                thread_id=thread_id, metadata={}, created_at=None,
            )
            for m in msgs
        ]

    async def fake_fetch_summaries(_conn, *, thread_id):
        return []

    async def fake_append(_conn, *, thread_id, role, content, metadata=None):
        store.threads.setdefault(thread_id, []).append(
            {"role": role, "content": content}
        )
        return uuid4()

    async def fake_bump(_conn, *, agent, version_label, content_template):
        store.prompt_version_seeded = True
        return SimpleNamespace(
            id=uuid4(), agent=agent, version_label=version_label,
            content_hash="fake", content_template=content_template,
        )

    monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.agents.quick.acquire", _fake_acquire)
    # ModelClient.call also hits the DB for active pricing — patch the
    # name bound inside the model_client module too.
    monkeypatch.setattr("wolfpaw.metering.model_client.acquire", _fake_acquire)
    monkeypatch.setattr(
        "wolfpaw.agents.quick.conv.get_or_create_thread",
        fake_get_or_create_thread,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.quick.conv.fetch_recent", fake_fetch_recent,
    )
    monkeypatch.setattr(
        "wolfpaw.agents.quick.conv.fetch_summaries", fake_fetch_summaries,
    )
    monkeypatch.setattr("wolfpaw.agents.quick.conv.append", fake_append)
    monkeypatch.setattr(
        "wolfpaw.agents.quick.bump_prompt_version", fake_bump,
    )

    # ModelClient writes a token_usage row + looks up pricing; stub both
    # so the suite doesn't need Postgres.
    async def fake_record(*args, **kwargs):
        return uuid4()

    async def fake_price(*args, **kwargs):
        return None  # ModelClient handles None as "0 cents, log warning"

    monkeypatch.setattr("wolfpaw.metering.model_client.record_usage", fake_record)
    monkeypatch.setattr(
        "wolfpaw.metering.model_client.get_active_price", fake_price,
    )

    yield store


# --- tests -----------------------------------------------------------------


async def test_single_turn_no_tools(agent_env):
    fake = FakeAnthropic([
        FakeTurn(content=[_text("2 + 2 is 4.")], stop_reason="end_turn"),
    ])
    agent = QuickAgent(model_client=ModelClient(anthropic=fake))
    ctx = ToolContext(user_id=uuid4())
    thread_id = uuid4()
    agent_env.threads[thread_id] = []

    text = await agent.handle(ctx=ctx, thread_id=thread_id, content="what's 2+2?")
    assert text == "2 + 2 is 4."

    # User + final assistant get persisted; nothing else.
    persisted = agent_env.threads[thread_id]
    assert [m["role"] for m in persisted] == ["user", "assistant"]
    assert persisted[0]["content"] == "what's 2+2?"
    assert persisted[1]["content"] == "2 + 2 is 4."


async def test_loop_runs_tool_then_returns_final_text(agent_env):
    fake = FakeAnthropic([
        FakeTurn(
            content=[
                _text("Let me calculate."),
                _tool_use("calculator", {"expression": "(3 + 4) * 2"}, "tu_a"),
            ],
            stop_reason="tool_use",
        ),
        FakeTurn(
            content=[_text("The answer is 14.")],
            stop_reason="end_turn",
        ),
    ])
    agent = QuickAgent(model_client=ModelClient(anthropic=fake))
    ctx = ToolContext(user_id=uuid4())
    thread_id = uuid4()
    agent_env.threads[thread_id] = []

    text = await agent.handle(
        ctx=ctx, thread_id=thread_id, content="compute (3+4)*2",
    )
    assert text == "The answer is 14."

    # Two model calls.
    assert len(fake.calls) == 2

    # Second call has the tool_result block in the message history.
    second_msgs = fake.calls[1]["messages"]
    last_user = second_msgs[-1]
    assert last_user["role"] == "user"
    tool_results = last_user["content"]
    assert any(
        isinstance(b, dict) and b.get("type") == "tool_result"
        and "14" in b.get("content", "")
        for b in tool_results
    )


async def test_unknown_tool_returns_error_to_model(agent_env):
    fake = FakeAnthropic([
        FakeTurn(
            content=[_tool_use("nonexistent", {}, "tu_x")],
            stop_reason="tool_use",
        ),
        FakeTurn(
            content=[_text("Couldn't do that.")],
            stop_reason="end_turn",
        ),
    ])
    agent = QuickAgent(model_client=ModelClient(anthropic=fake))
    ctx = ToolContext(user_id=uuid4())
    thread_id = uuid4()
    agent_env.threads[thread_id] = []

    text = await agent.handle(ctx=ctx, thread_id=thread_id, content="do nothing")
    assert text == "Couldn't do that."

    # Tool result echoed back to model marked as error.
    second_msgs = fake.calls[1]["messages"]
    last_user = second_msgs[-1]
    tool_result = last_user["content"][0]
    assert tool_result["is_error"] is True
    assert "not available" in tool_result["content"]


async def test_sandbox_tools_blocked_from_quick_agent(agent_env):
    fake = FakeAnthropic([
        FakeTurn(
            content=[_tool_use("run_python", {"code": "print('hi')"}, "tu_p")],
            stop_reason="tool_use",
        ),
        FakeTurn(
            content=[_text("Can't do that here.")],
            stop_reason="end_turn",
        ),
    ])
    agent = QuickAgent(model_client=ModelClient(anthropic=fake))
    ctx = ToolContext(user_id=uuid4())
    thread_id = uuid4()
    agent_env.threads[thread_id] = []

    await agent.handle(ctx=ctx, thread_id=thread_id, content="run code")
    tool_result = fake.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "not available" in tool_result["content"]


async def test_iteration_cap_returns_friendly_message(agent_env):
    # Endless tool_use loop.
    turns = [
        FakeTurn(
            content=[_tool_use("calculator", {"expression": "1+1"}, f"tu_{i}")],
            stop_reason="tool_use",
        )
        for i in range(20)
    ]
    fake = FakeAnthropic(turns)
    agent = QuickAgent(
        model_client=ModelClient(anthropic=fake),
        max_iterations=3,
    )
    ctx = ToolContext(user_id=uuid4())
    thread_id = uuid4()
    agent_env.threads[thread_id] = []

    text = await agent.handle(ctx=ctx, thread_id=thread_id, content="loop forever")
    assert "ran out of steps" in text
    assert len(fake.calls) == 3


async def test_passes_full_history_to_subsequent_call(agent_env):
    """A second user turn should include the prior turns in the messages list."""
    fake1 = FakeAnthropic([
        FakeTurn(content=[_text("First answer.")], stop_reason="end_turn"),
    ])
    agent = QuickAgent(model_client=ModelClient(anthropic=fake1))
    ctx = ToolContext(user_id=uuid4())
    thread_id = uuid4()
    agent_env.threads[thread_id] = []
    await agent.handle(ctx=ctx, thread_id=thread_id, content="first")

    fake2 = FakeAnthropic([
        FakeTurn(content=[_text("Second answer.")], stop_reason="end_turn"),
    ])
    agent2 = QuickAgent(model_client=ModelClient(anthropic=fake2))
    await agent2.handle(ctx=ctx, thread_id=thread_id, content="second")

    second_call_msgs = fake2.calls[0]["messages"]
    # Past: user "first", assistant "First answer." — plus new user "second".
    roles_contents = [(m["role"], m["content"]) for m in second_call_msgs]
    assert roles_contents == [
        ("user", "first"),
        ("assistant", "First answer."),
        ("user", "second"),
    ]


async def test_emit_callback_fires_on_each_tool_use(agent_env):
    fake = FakeAnthropic([
        FakeTurn(
            content=[
                _tool_use("calculator", {"expression": "1+1"}, "tu_1"),
                _tool_use("calculator", {"expression": "2+2"}, "tu_2"),
            ],
            stop_reason="tool_use",
        ),
        FakeTurn(content=[_text("done")], stop_reason="end_turn"),
    ])
    agent = QuickAgent(model_client=ModelClient(anthropic=fake))
    ctx = ToolContext(user_id=uuid4())
    thread_id = uuid4()
    agent_env.threads[thread_id] = []

    emitted: list[tuple[str, str]] = []

    async def emit(event, data):
        emitted.append((event, data))

    await agent.handle(
        ctx=ctx, thread_id=thread_id, content="calc both", emit=emit,
    )
    tool_events = [e for e in emitted if e[0] == "tool"]
    assert len(tool_events) == 2
    assert all("calculator" in e[1] for e in tool_events)


async def test_only_visible_turns_persisted(agent_env):
    """Tool calls + tool results live in-process; persistence only stores
    the user message + final assistant text."""
    fake = FakeAnthropic([
        FakeTurn(
            content=[_tool_use("calculator", {"expression": "1+1"}, "tu_1")],
            stop_reason="tool_use",
        ),
        FakeTurn(content=[_text("It's 2.")], stop_reason="end_turn"),
    ])
    agent = QuickAgent(model_client=ModelClient(anthropic=fake))
    ctx = ToolContext(user_id=uuid4())
    thread_id = uuid4()
    agent_env.threads[thread_id] = []

    await agent.handle(ctx=ctx, thread_id=thread_id, content="one plus one")
    persisted = agent_env.threads[thread_id]
    assert [m["role"] for m in persisted] == ["user", "assistant"]
    assert persisted[-1]["content"] == "It's 2."


# --- truncation ------------------------------------------------------------


async def test_truncated_tool_call_is_not_reported_as_success(agent_env):
    """Regression: saving a document silently did nothing.

    When a tool call's arguments run past `max_tokens`, Anthropic returns
    `stop_reason="max_tokens"` and a `tool_use` block whose `input` JSON was
    cut off mid-generation — here a `write_doc` with a filename and no
    content. The agent used to fall through to `return result.text`, which is
    the preamble the model emitted *before* starting the call ("I'll save
    that for you"), so the user was told it worked and no file existed.

    The truncated call must not run, and the reply must not claim success.
    """
    fake = FakeAnthropic([
        FakeTurn(
            content=[
                _text("I'll save that for you."),
                # Truncated: filename survived, content never made it out.
                _tool_use("write_doc", {"filename": "resume-01.md"}, "tu_t"),
            ],
            stop_reason="max_tokens",
        ),
    ])
    agent = QuickAgent(model_client=ModelClient(anthropic=fake))
    ctx = ToolContext(user_id=uuid4())
    thread_id = uuid4()
    agent_env.threads[thread_id] = []

    text = await agent.handle(
        ctx=ctx, thread_id=thread_id, content="save my resume",
    )

    assert "write_doc" in text
    assert "nothing was saved" in text
    # The misleading preamble must not be what the user is left with.
    assert text != "I'll save that for you."
    # And the truncated call must never have been dispatched.
    assert len(fake.calls) == 1


async def test_truncated_prose_is_labelled_rather_than_passed_off(agent_env):
    fake = FakeAnthropic([
        FakeTurn(content=[_text("Here is the first half")],
                 stop_reason="max_tokens"),
    ])
    agent = QuickAgent(model_client=ModelClient(anthropic=fake))
    ctx = ToolContext(user_id=uuid4())
    thread_id = uuid4()
    agent_env.threads[thread_id] = []

    text = await agent.handle(ctx=ctx, thread_id=thread_id, content="write an essay")
    assert text.startswith("Here is the first half")
    assert "truncated" in text


async def test_model_calls_use_the_configured_token_budget(agent_env):
    """The budget has to be big enough for a tool call carrying a document —
    1024 was not, which is what truncated the write_doc above."""
    fake = FakeAnthropic([
        FakeTurn(content=[_text("hi")], stop_reason="end_turn"),
    ])
    agent = QuickAgent(model_client=ModelClient(anthropic=fake))
    ctx = ToolContext(user_id=uuid4())
    thread_id = uuid4()
    agent_env.threads[thread_id] = []

    await agent.handle(ctx=ctx, thread_id=thread_id, content="hi")
    assert fake.calls[-1]["max_tokens"] >= 8192
