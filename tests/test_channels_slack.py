"""HTTP-level tests for the Slack channel — OAuth, events, slash commands.

The Slack API client is swapped for a FakeSlackClient that captures
sent messages + returns canned OAuth responses. DB access is stubbed
via fake_acquire + DAO patching so the suite runs without Postgres."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from wolfpaw.api import create_app
from wolfpaw.auth.deps import require_user_id
from wolfpaw.channels.slack_client import (
    FakeSlackClient,
    reset_slack_client,
    set_slack_client,
)
from wolfpaw.memory import channel_links, slack_workspaces


_SIGNING_SECRET = "test-signing-secret-1234567890"


@asynccontextmanager
async def _fake_acquire():
    yield None


@pytest.fixture
def fake_slack(monkeypatch):
    """Install a fresh FakeSlackClient and stub DB access."""
    fake = FakeSlackClient()
    set_slack_client(fake)
    monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.channels.slack.acquire", _fake_acquire)

    monkeypatch.setenv("WOLFPAW_SLACK_SIGNING_SECRET", _SIGNING_SECRET)
    monkeypatch.setenv("WOLFPAW_SLACK_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("WOLFPAW_SLACK_CLIENT_SECRET", "test-client-secret")
    from wolfpaw.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]

    yield fake
    reset_slack_client()
    get_settings.cache_clear()  # type: ignore[attr-defined]


def _client(uid: UUID | None = None) -> TestClient:
    app = create_app()
    if uid is not None:
        app.dependency_overrides[require_user_id] = lambda: uid
    return TestClient(app)


def _sign(body: bytes, ts: str | None = None) -> dict[str, str]:
    """Return headers Slack would send for a request with `body`."""
    if ts is None:
        ts = str(int(time.time()))
    base = b"v0:" + ts.encode() + b":" + body
    sig = "v0=" + hmac.new(
        _SIGNING_SECRET.encode(), base, hashlib.sha256,
    ).hexdigest()
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": sig,
    }


# --- /install-url ---------------------------------------------------------


def test_install_url_requires_auth(fake_slack):
    app = create_app()
    client = TestClient(app)
    r = client.get("/channels/slack/install-url")
    assert r.status_code == 401


def test_install_url_returns_slack_oauth_url(monkeypatch, fake_slack):
    user_id = uuid4()

    async def fake_issue(_conn, *, user_id, channel, ttl_minutes):
        return "fake-state-token"

    monkeypatch.setattr(
        "wolfpaw.channels.slack.issue_state_token", fake_issue,
    )

    client = _client(uid=user_id)
    r = client.get("/channels/slack/install-url")
    assert r.status_code == 200
    body = r.json()
    assert body["url"].startswith("https://slack.com/oauth/v2/authorize")
    assert "client_id=test-client-id" in body["url"]
    assert "state=fake-state-token" in body["url"]
    assert "/channels/slack/oauth/callback" in body["url"]


def test_install_url_503_when_client_id_unset(monkeypatch, fake_slack):
    monkeypatch.setenv("WOLFPAW_SLACK_CLIENT_ID", "")
    from wolfpaw.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]

    client = _client(uid=uuid4())
    r = client.get("/channels/slack/install-url")
    assert r.status_code == 503


# --- /oauth/callback ------------------------------------------------------


def _oauth_payload(team_id="T123", team_name="Acme", bot_user_id="U_BOT",
                   slack_user_id="U_INSTALLER", bot_token="xoxb-test"):
    return {
        "ok": True,
        "access_token": bot_token,
        "bot_user_id": bot_user_id,
        "team": {"id": team_id, "name": team_name},
        "authed_user": {"id": slack_user_id, "name": "installer"},
    }


def test_oauth_callback_persists_workspace_and_link(monkeypatch, fake_slack):
    """Happy path: state token consumed → code exchanged → workspace
    + channel_link persisted."""
    wolfpaw_user_id = uuid4()
    fake_slack.oauth_response = _oauth_payload(
        team_id="TZZZ", team_name="Wolfpaw HQ",
        bot_user_id="U_BOT_A", slack_user_id="U_ALICE",
    )

    async def fake_consume(_conn, *, plain, channel):
        assert plain == "valid-state"
        assert channel == "slack"
        return wolfpaw_user_id

    upserts: list[dict] = []
    links: list[dict] = []

    async def fake_upsert(_conn, **kw):
        upserts.append(kw)

        class _W: pass
        return _W()

    async def fake_create_link(_conn, **kw):
        links.append(kw)

        class _L: pass
        return _L()

    monkeypatch.setattr("wolfpaw.channels.slack.consume_state_token", fake_consume)
    monkeypatch.setattr(slack_workspaces, "upsert", fake_upsert)
    monkeypatch.setattr(channel_links, "create", fake_create_link)

    client = _client()
    r = client.get(
        "/channels/slack/oauth/callback?code=auth-code&state=valid-state",
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "Wolfpaw is now installed" in r.text
    # Exchanged the code with Slack.
    assert len(fake_slack.oauth_calls) == 1
    assert fake_slack.oauth_calls[0]["code"] == "auth-code"
    # Workspace persisted with the bot token.
    assert len(upserts) == 1
    assert upserts[0]["team_id"] == "TZZZ"
    assert upserts[0]["bot_token"] == "xoxb-test"
    assert upserts[0]["installed_by_user_id"] == wolfpaw_user_id
    # Channel link uses the composite external_id.
    assert len(links) == 1
    assert links[0]["external_id"] == "TZZZ:U_ALICE"
    assert links[0]["user_id"] == wolfpaw_user_id


def test_oauth_callback_bad_state_shows_error_page(monkeypatch, fake_slack):
    from wolfpaw.channels.telegram_tokens import LinkTokenError

    async def fake_consume(_conn, *, plain, channel):
        raise LinkTokenError("token expired")

    monkeypatch.setattr("wolfpaw.channels.slack.consume_state_token", fake_consume)

    client = _client()
    r = client.get(
        "/channels/slack/oauth/callback?code=auth-code&state=bad-state",
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "expired" in r.text.lower() or "already used" in r.text.lower()
    # Slack was NOT contacted with the bad state.
    assert len(fake_slack.oauth_calls) == 0


def test_oauth_callback_user_cancellation_shown_friendly(monkeypatch, fake_slack):
    """Slack returns ?error=access_denied when the user clicks Cancel."""
    client = _client()
    r = client.get(
        "/channels/slack/oauth/callback?error=access_denied&state=anything",
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "access_denied" in r.text


def test_oauth_callback_missing_code_or_state(fake_slack):
    client = _client()
    r = client.get("/channels/slack/oauth/callback?code=x", follow_redirects=False)
    assert r.status_code == 400


# --- /events --------------------------------------------------------------


def test_events_url_verification_echoes_challenge(fake_slack):
    body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
    headers = _sign(body)

    client = _client()
    r = client.post(
        "/channels/slack/events", content=body,
        headers={**headers, "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert r.text == "abc123"


def test_events_rejects_bad_signature(fake_slack):
    body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
    client = _client()
    r = client.post(
        "/channels/slack/events", content=body,
        headers={
            "X-Slack-Request-Timestamp": str(int(time.time())),
            "X-Slack-Signature": "v0=deadbeef",
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 403


def test_events_ignores_non_im_messages(monkeypatch, fake_slack):
    """A message in a channel (channel_type != 'im') should be 200'd and
    NOT dispatched — DMs are the only path in v1."""
    body = json.dumps({
        "type": "event_callback",
        "team_id": "T1",
        "event": {
            "type": "message", "channel_type": "channel",
            "user": "U1", "channel": "C1", "text": "hi",
        },
    }).encode()
    headers = _sign(body)

    # If find_user got called we'd hit the unstubbed DAO; assert it isn't.
    calls = {"find_user": 0}

    async def fake_find_user(_conn, *, channel, external_id):
        calls["find_user"] += 1
        return None

    monkeypatch.setattr(channel_links, "find_user", fake_find_user)

    client = _client()
    r = client.post(
        "/channels/slack/events", content=body,
        headers={**headers, "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert calls["find_user"] == 0


def test_events_ignores_bot_authored_messages(monkeypatch, fake_slack):
    """Our own replies show up in the events stream too. Filter them
    out — bot_id set OR subtype == 'bot_message'."""
    body = json.dumps({
        "type": "event_callback",
        "team_id": "T1",
        "event": {
            "type": "message", "channel_type": "im",
            "user": "U1", "channel": "D1", "text": "hi",
            "bot_id": "B_SELF",
        },
    }).encode()
    headers = _sign(body)

    calls = {"find_user": 0}

    async def fake_find_user(_conn, *, channel, external_id):
        calls["find_user"] += 1
        return None

    monkeypatch.setattr(channel_links, "find_user", fake_find_user)

    client = _client()
    r = client.post(
        "/channels/slack/events", content=body,
        headers={**headers, "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert calls["find_user"] == 0


def test_events_unlinked_user_gets_helpful_reply(monkeypatch, fake_slack):
    """DM from a slack user who isn't linked to Wolfpaw → bot replies
    with a 'install from web' prompt (only if we have the workspace's
    bot token)."""

    async def fake_find_user(_conn, *, channel, external_id):
        return None

    class _Workspace:
        bot_token = "xoxb-test"
        bot_user_id = "U_BOT"
        team_id = "T1"
        team_name = "Test"

    async def fake_get_workspace(_conn, *, team_id):
        return _Workspace()

    monkeypatch.setattr(channel_links, "find_user", fake_find_user)
    monkeypatch.setattr(slack_workspaces, "get", fake_get_workspace)

    body = json.dumps({
        "type": "event_callback",
        "team_id": "T1",
        "event": {
            "type": "message", "channel_type": "im",
            "user": "U_NEW", "channel": "D1", "text": "hi",
        },
    }).encode()
    headers = _sign(body)

    client = _client()
    r = client.post(
        "/channels/slack/events", content=body,
        headers={**headers, "Content-Type": "application/json"},
    )
    assert r.status_code == 200

    async def _wait():
        for _ in range(50):
            if fake_slack.sent:
                return
            await asyncio.sleep(0.01)

    asyncio.get_event_loop().run_until_complete(_wait())
    assert any("install" in s["text"].lower() for s in fake_slack.sent)


# --- /commands ------------------------------------------------------------


def _command_form(team_id="T1", user_id="U_ALICE", channel_id="C_GENERAL",
                  text="/help"):
    """Build the form-encoded body Slack POSTs for slash commands."""
    return (
        f"team_id={team_id}&user_id={user_id}&channel_id={channel_id}"
        f"&text={text}&command=%2Fwolfpaw"
    ).encode()


def test_commands_rejects_bad_signature(fake_slack):
    body = _command_form()
    client = _client()
    r = client.post(
        "/channels/slack/commands", content=body,
        headers={
            "X-Slack-Request-Timestamp": str(int(time.time())),
            "X-Slack-Signature": "v0=deadbeef",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert r.status_code == 403


def test_commands_unlinked_user_gets_ephemeral_prompt(monkeypatch, fake_slack):
    async def fake_find_user(_conn, *, channel, external_id):
        return None

    monkeypatch.setattr(channel_links, "find_user", fake_find_user)

    body = _command_form(text="/help")
    headers = _sign(body)

    client = _client()
    r = client.post(
        "/channels/slack/commands", content=body,
        headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["response_type"] == "ephemeral"
    assert "isn't linked" in payload["text"]


def test_commands_dispatches_slash_command(monkeypatch, fake_slack):
    """`/wolfpaw /help` should route through the dispatcher and return
    the /help output inline (no Router call, no chat.postMessage)."""
    wolfpaw_user = uuid4()

    async def fake_find_user(_conn, *, channel, external_id):
        return wolfpaw_user

    monkeypatch.setattr(channel_links, "find_user", fake_find_user)

    body = _command_form(text="%2Fhelp")  # url-encoded "/help"
    headers = _sign(body)

    client = _client()
    r = client.post(
        "/channels/slack/commands", content=body,
        headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200
    payload = r.json()
    assert "Available commands" in payload["text"]
    # No outbound chat.postMessage — slash commands respond inline.
    assert fake_slack.sent == []


def test_commands_empty_text_treated_as_help(monkeypatch, fake_slack):
    """`/wolfpaw` with no args → behave like `/help` rather than confusing the user."""

    async def fake_find_user(_conn, *, channel, external_id):
        return uuid4()

    monkeypatch.setattr(channel_links, "find_user", fake_find_user)

    body = _command_form(text="")
    headers = _sign(body)

    client = _client()
    r = client.post(
        "/channels/slack/commands", content=body,
        headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200
    assert "Available commands" in r.json()["text"]


def test_commands_freeform_text_acks_inline_and_fires_router(monkeypatch, fake_slack):
    """Non-slash text → return an ephemeral 'working on it', kick off the
    Router as a background task that ultimately posts via chat.postMessage."""
    wolfpaw_user = uuid4()

    async def fake_find_user(_conn, *, channel, external_id):
        return wolfpaw_user

    class _Workspace:
        bot_token = "xoxb-test"
        bot_user_id = "U_BOT"

    async def fake_get_workspace(_conn, *, team_id):
        return _Workspace()

    async def fake_resolve_thread(_conn, _user_id):
        return uuid4()

    class FakeRouter:
        async def handle(self, *, ctx, thread_id, content, emit=None):
            return f"router got: {content}"

    monkeypatch.setattr(channel_links, "find_user", fake_find_user)
    monkeypatch.setattr(slack_workspaces, "get", fake_get_workspace)
    monkeypatch.setattr(
        "wolfpaw.channels.slack._resolve_slack_thread", fake_resolve_thread,
    )
    monkeypatch.setattr(
        "wolfpaw.channels.slack.get_router", lambda: FakeRouter(),
    )

    body = _command_form(text="what+is+2+plus+2")
    headers = _sign(body)

    client = _client()
    r = client.post(
        "/channels/slack/commands", content=body,
        headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["response_type"] == "ephemeral"
    assert "working on it" in payload["text"].lower()

    async def _wait():
        for _ in range(50):
            if fake_slack.sent:
                return
            await asyncio.sleep(0.01)

    asyncio.get_event_loop().run_until_complete(_wait())
    assert len(fake_slack.sent) == 1
    assert "router got:" in fake_slack.sent[0]["text"]


def test_commands_revoked_workspace_friendly_error(monkeypatch, fake_slack):
    """slack_workspaces.get returns None for a revoked install → ephemeral
    prompt to re-install."""

    async def fake_find_user(_conn, *, channel, external_id):
        return uuid4()

    async def fake_get_workspace(_conn, *, team_id):
        return None

    monkeypatch.setattr(channel_links, "find_user", fake_find_user)
    monkeypatch.setattr(slack_workspaces, "get", fake_get_workspace)

    body = _command_form(text="hello+world")
    headers = _sign(body)

    client = _client()
    r = client.post(
        "/channels/slack/commands", content=body,
        headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200
    assert "revoked" in r.json()["text"].lower() or "re-install" in r.json()["text"].lower()
    assert fake_slack.sent == []
