"""HTTP-level tests for the web channel's `/channels/web/chat` SSE endpoint.

Auth is bypassed via dependency override so these tests don't need the DB.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from wolfpaw.api import create_app
from wolfpaw.auth.deps import require_user_id


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


def test_chat_plain_message_returns_stub_delta():
    client = _client()
    r = client.post(
        "/channels/web/chat", json={"content": "hello wolfpaw"}
    )
    events = _parse_events(r.text)
    kinds = [e for e, _ in events]
    assert "delta" in kinds
    assert kinds[-1] == "done"
    payload = next(data for ev, data in events if ev == "delta")
    assert "Agent pipeline" in payload


def test_chat_preserves_trace_id_header():
    client = _client()
    r = client.post(
        "/channels/web/chat",
        json={"content": "/help"},
        headers={"x-trace-id": "a" * 32},
    )
    assert r.headers["x-trace-id"] == "a" * 32
