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
):
    """Patch the web channel's router + thread resolution so plain-message
    tests run without Postgres or Anthropic. Returns the (fake) thread_id
    the endpoint will emit."""
    thread_id = uuid4()

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
        "wolfpaw.channels.web.conv.get_or_create_thread",
        lambda _conn, **_kw: _async_return(thread_id),
    )
    return thread_id


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


def test_chat_agent_exception_surfaces_as_error_event(monkeypatch):
    class BoomRouter:
        async def handle(self, **_kwargs):
            raise RuntimeError("boom")

    thread_id = uuid4()
    monkeypatch.setattr("wolfpaw.channels.web.get_router", lambda: BoomRouter())
    monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.channels.web.acquire", _fake_acquire)
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


def test_chat_preserves_trace_id_header():
    client = _client()
    r = client.post(
        "/channels/web/chat",
        json={"content": "/help"},
        headers={"x-trace-id": "a" * 32},
    )
    assert r.headers["x-trace-id"] == "a" * 32
