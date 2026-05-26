"""HTTP-level tests for the web channel's `/channels/web/chat` SSE endpoint.

Auth is bypassed via dependency override so these tests don't need the DB.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from wolfpaw.api import create_app
from wolfpaw.auth.deps import require_user_id


@asynccontextmanager
async def _fake_acquire():
    yield None


def _client(user_id: UUID | None = None) -> TestClient:
    app = create_app()
    uid = user_id or uuid4()
    app.dependency_overrides[require_user_id] = lambda: uid
    return TestClient(app)


def _parse_events(body: str) -> list[tuple[str, str]]:
    """Parse an SSE response body into (event, data) tuples."""
    events: list[tuple[str, str]] = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event = ""
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data_lines.append(line.removeprefix("data: "))
            elif line == "data:":
                data_lines.append("")
        events.append((event, "\n".join(data_lines)))
    return events


def test_chat_requires_auth():
    """Without the dependency override, the endpoint must 401."""
    app = create_app()
    client = TestClient(app)
    r = client.post("/channels/web/chat", json={"content": "hi"})
    assert r.status_code == 401


def test_chat_slash_command_streams_command_event():
    client = _client()
    r = client.post("/channels/web/chat", json={"content": "/help"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _parse_events(r.text)
    kinds = [e for e, _ in events]
    assert "command" in kinds
    assert kinds[-1] == "done"
    command_payload = next(data for ev, data in events if ev == "command")
    assert "Available commands:" in command_payload
    assert "/help" in command_payload


def test_chat_unknown_command_does_not_fall_through():
    client = _client()
    r = client.post(
        "/channels/web/chat", json={"content": "/notreal"}
    )
    events = _parse_events(r.text)
    kinds = [e for e, _ in events]
    assert "command" in kinds
    assert "delta" not in kinds  # must not invoke the (nonexistent) agent
    payload = next(data for ev, data in events if ev == "command")
    assert "Unknown command" in payload


def _stub_router_and_thread(
    monkeypatch, *, canned_reply: str, events_to_emit=(),
    existing_thread_id: UUID | None = None,
):
    """Patch the web channel's router + thread resolution so plain-message
    tests run without Postgres or Anthropic.

    `existing_thread_id`:
      - None (default) → simulate a fresh user; `get_most_recent_thread`
        returns None, so the endpoint mints via `get_or_create_thread`
        and the returned thread_id is what the test asserts on.
      - UUID → simulate "this user already has a thread on web"; the
        endpoint resolves to it without minting.
    """
    minted_thread_id = uuid4()
    effective_thread_id = existing_thread_id or minted_thread_id

    class FakeRouter:
        async def handle(self, *, ctx, thread_id, content, emit=None):
            for ev, data in events_to_emit:
                if emit:
                    await emit(ev, data)
            return canned_reply

    monkeypatch.setattr("wolfpaw.channels.web.get_router", lambda: FakeRouter())
    monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.channels.web.acquire", _fake_acquire)
    monkeypatch.setattr(
        "wolfpaw.channels.web.conv.get_most_recent_thread",
        lambda _conn, **_kw: _async_return(existing_thread_id),
    )
    monkeypatch.setattr(
        "wolfpaw.channels.web.conv.get_or_create_thread",
        lambda _conn, **_kw: _async_return(minted_thread_id),
    )
    return effective_thread_id


async def _async_return(value):
    return value


def test_chat_plain_message_runs_through_router(monkeypatch):
    thread_id = _stub_router_and_thread(
        monkeypatch,
        canned_reply="2 + 2 is 4.",
        events_to_emit=[
            ("triage", "quick — simple lookup"),
            ("tool", "calculator(expression='2+2')"),
        ],
    )
    client = _client()
    r = client.post(
        "/channels/web/chat", json={"content": "what's 2+2?"},
    )
    events = _parse_events(r.text)
    kinds = [e for e, _ in events]
    # New stream shape: thread → triage → tool → delta → done.
    assert kinds[0] == "thread"
    assert "triage" in kinds
    assert "tool" in kinds
    assert "delta" in kinds
    assert kinds[-1] == "done"
    thread_payload = next(d for e, d in events if e == "thread")
    assert thread_payload == str(thread_id)
    triage_payload = next(d for e, d in events if e == "triage")
    assert "quick" in triage_payload
    delta_payload = next(d for e, d in events if e == "delta")
    assert delta_payload == "2 + 2 is 4."


def test_chat_plain_message_no_tools_still_streams_delta(monkeypatch):
    _stub_router_and_thread(
        monkeypatch, canned_reply="hello back",
        events_to_emit=[("triage", "quick — greeting")],
    )
    client = _client()
    r = client.post(
        "/channels/web/chat", json={"content": "hi"},
    )
    events = _parse_events(r.text)
    kinds = [e for e, _ in events]
    assert kinds == ["thread", "triage", "delta", "done"]
    assert next(d for e, d in events if e == "delta") == "hello back"


def test_chat_without_thread_id_picks_up_most_recent_thread(monkeypatch):
    """Cross-device continuity: a request that doesn't supply a thread_id
    (new browser, new tab, different device) resolves to the user's
    most-recent web thread rather than minting a fresh one. The thread
    event emits the existing id."""
    existing = uuid4()
    _stub_router_and_thread(
        monkeypatch, canned_reply="picked up where we left off",
        existing_thread_id=existing,
    )
    client = _client()
    r = client.post("/channels/web/chat", json={"content": "what was that?"})
    events = _parse_events(r.text)
    thread_payload = next(d for e, d in events if e == "thread")
    assert thread_payload == str(existing)


def test_chat_with_explicit_thread_id_honors_it(monkeypatch):
    """When the client DOES supply a thread_id, the endpoint honors it
    (no most-recent fallback) — get_most_recent_thread is never
    consulted for the explicit case."""
    explicit = uuid4()
    called: dict[str, int] = {"most_recent": 0, "get_or_create": 0}

    async def fake_most_recent(_conn, **_kw):
        called["most_recent"] += 1
        return None

    async def fake_get_or_create(_conn, **kw):
        called["get_or_create"] += 1
        return kw["thread_id"]      # echo back so the assertion holds

    class FakeRouter:
        async def handle(self, **_kwargs):
            return "ok"

    monkeypatch.setattr("wolfpaw.channels.web.get_router", lambda: FakeRouter())
    monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.channels.web.acquire", _fake_acquire)
    monkeypatch.setattr(
        "wolfpaw.channels.web.conv.get_most_recent_thread", fake_most_recent,
    )
    monkeypatch.setattr(
        "wolfpaw.channels.web.conv.get_or_create_thread", fake_get_or_create,
    )

    client = _client()
    r = client.post(
        "/channels/web/chat",
        json={"content": "hi", "thread_id": str(explicit)},
    )
    events = _parse_events(r.text)
    thread_payload = next(d for e, d in events if e == "thread")
    assert thread_payload == str(explicit)
    # most-recent must NOT be consulted when the client provided one.
    assert called["most_recent"] == 0
    assert called["get_or_create"] == 1


def test_chat_agent_exception_surfaces_as_error_event(monkeypatch):
    class BoomRouter:
        async def handle(self, **_kwargs):
            raise RuntimeError("boom")

    thread_id = uuid4()
    monkeypatch.setattr("wolfpaw.channels.web.get_router", lambda: BoomRouter())
    monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.channels.web.acquire", _fake_acquire)
    monkeypatch.setattr(
        "wolfpaw.channels.web.conv.get_most_recent_thread",
        lambda _conn, **_kw: _async_return(None),
    )
    monkeypatch.setattr(
        "wolfpaw.channels.web.conv.get_or_create_thread",
        lambda _conn, **_kw: _async_return(thread_id),
    )

    client = _client()
    r = client.post("/channels/web/chat", json={"content": "hi"})
    events = _parse_events(r.text)
    kinds = [e for e, _ in events]
    assert "error" in kinds
    assert kinds[-1] == "done"
    err = next(d for e, d in events if e == "error")
    assert "boom" in err


def test_help_lists_usage_command():
    """Confirms `metering.usage_report` was imported at app boot so /usage
    registered itself with the dispatcher."""
    client = _client()
    r = client.post("/channels/web/chat", json={"content": "/help"})
    assert "/usage" in r.text


def test_chat_reset_emits_reset_event_before_command(monkeypatch):
    """`/reset` from web should emit a `reset` SSE event so the client
    drops its thread_id, followed by the usual command + done events.

    `/reset` now also mints a fresh thread server-side (because channels
    resolve to the user's most-recent thread when no thread_id is
    supplied — without the server-side mint, the next message would
    land back in the previous thread). Stub the DAO call out."""
    monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
    monkeypatch.setattr(
        "wolfpaw.memory.conversational.get_or_create_thread",
        lambda _conn, **_kw: _async_return(uuid4()),
    )

    client = _client()
    r = client.post("/channels/web/chat", json={"content": "/reset"})
    assert r.status_code == 200
    events = _parse_events(r.text)
    kinds = [e for e, _ in events]
    assert "reset" in kinds
    assert "command" in kinds
    assert kinds.index("reset") < kinds.index("command")
    assert kinds[-1] == "done"


def test_chat_preserves_trace_id_header():
    client = _client()
    r = client.post(
        "/channels/web/chat",
        json={"content": "/help"},
        headers={"x-trace-id": "a" * 32},
    )
    assert r.headers["x-trace-id"] == "a" * 32
