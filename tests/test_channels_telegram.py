"""HTTP-level tests for the Telegram channel — webhook + link-token.

The Bot API client is swapped for a FakeTelegramClient that captures
sent messages. DB access is stubbed via fake_acquire + DAO patching so
the suite runs without Postgres."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from wolfpaw.api import create_app
from wolfpaw.auth.deps import require_user_id
from wolfpaw.channels.telegram_client import (
    FakeTelegramClient,
    reset_telegram_client,
    set_telegram_client,
)
from wolfpaw.memory import channel_links
from wolfpaw.memory.channel_links import ChannelLink


@asynccontextmanager
async def _fake_acquire():
    yield None


@pytest.fixture
def fake_telegram(monkeypatch):
    """Install a fresh FakeTelegramClient and stub DB access."""
    fake = FakeTelegramClient()
    set_telegram_client(fake)
    monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.channels.telegram.acquire", _fake_acquire)
    yield fake
    reset_telegram_client()


def _client(uid: UUID | None = None) -> TestClient:
    """TestClient with `require_user_id` bypassed."""
    app = create_app()
    if uid is not None:
        app.dependency_overrides[require_user_id] = lambda: uid
    return TestClient(app)


# --- /link-token ----------------------------------------------------------


def test_link_token_requires_auth(monkeypatch, fake_telegram):
    monkeypatch.setenv("WOLFPAW_TELEGRAM_BOT_USERNAME", "WolfpawBotTest")
    from wolfpaw.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    app = create_app()
    client = TestClient(app)
    r = client.post("/channels/telegram/link-token")
    assert r.status_code == 401
    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_link_token_returns_deep_link(monkeypatch, fake_telegram):
    minted_tokens: list[dict] = []

    async def fake_issue(_conn, *, user_id, channel, ttl_minutes):
        minted_tokens.append({
            "user_id": user_id, "channel": channel,
            "ttl_minutes": ttl_minutes,
        })
        return "TEST_PLAIN_TOKEN_123"

    monkeypatch.setattr(
        "wolfpaw.channels.telegram.issue_link_token", fake_issue,
    )
    monkeypatch.setenv("WOLFPAW_TELEGRAM_BOT_USERNAME", "WolfpawBotTest")
    from wolfpaw.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    uid = uuid4()
    client = _client(uid)
    r = client.post("/channels/telegram/link-token")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"] == (
        "https://t.me/WolfpawBotTest?start=link_TEST_PLAIN_TOKEN_123"
    )
    assert body["expires_in_minutes"] > 0
    assert len(minted_tokens) == 1
    assert minted_tokens[0]["user_id"] == uid
    assert minted_tokens[0]["channel"] == "telegram"
    get_settings.cache_clear()  # type: ignore[attr-defined]


# --- /webhook helpers -----------------------------------------------------


def _message_update(
    *, text: str, from_id: int = 1001, chat_id: int | None = None,
    username: str | None = "alice",
) -> dict:
    return {
        "update_id": 42,
        "message": {
            "message_id": 1, "date": int(datetime.now(timezone.utc).timestamp()),
            "from": {
                "id": from_id, "is_bot": False, "first_name": "Test",
                "username": username,
            },
            "chat": {"id": chat_id or from_id, "type": "private"},
            "text": text,
        },
    }


# --- /webhook -------------------------------------------------------------


def test_webhook_rejects_bad_secret(monkeypatch, fake_telegram):
    monkeypatch.setenv("WOLFPAW_TELEGRAM_WEBHOOK_SECRET", "expected-secret")
    from wolfpaw.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]

    app = create_app()
    client = TestClient(app)
    r = client.post(
        "/channels/telegram/webhook",
        json=_message_update(text="hello"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
    )
    assert r.status_code == 403
    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_webhook_accepts_matching_secret(monkeypatch, fake_telegram):
    monkeypatch.setenv("WOLFPAW_TELEGRAM_WEBHOOK_SECRET", "expected-secret")
    from wolfpaw.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]

    async def fake_find_user(_conn, *, channel, external_id):
        return None  # unlinked → friendly prompt

    monkeypatch.setattr(channel_links, "find_user", fake_find_user)

    app = create_app()
    client = TestClient(app)
    r = client.post(
        "/channels/telegram/webhook",
        json=_message_update(text="hello"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "expected-secret"},
    )
    assert r.status_code == 200
    # Unlinked user got a "connect first" message.
    assert len(fake_telegram.sent) == 1
    assert "don't recognize" in fake_telegram.sent[0]["text"]
    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_webhook_ignores_non_message_updates(monkeypatch, fake_telegram):
    """Callback queries, inline queries, etc. should ack without doing
    anything."""
    app = create_app()
    client = TestClient(app)
    r = client.post(
        "/channels/telegram/webhook",
        json={"update_id": 1, "callback_query": {"id": "cb1"}},
    )
    assert r.status_code == 200
    assert fake_telegram.sent == []


def test_webhook_handles_bare_start_with_friendly_prompt(monkeypatch, fake_telegram):
    """Bare `/start` (no link payload) shouldn't error — guide the user."""
    app = create_app()
    client = TestClient(app)
    r = client.post(
        "/channels/telegram/webhook",
        json=_message_update(text="/start"),
    )
    assert r.status_code == 200
    assert len(fake_telegram.sent) == 1
    assert "link this Telegram account first" in fake_telegram.sent[0]["text"]


def test_webhook_unlinked_user_gets_connect_prompt(monkeypatch, fake_telegram):
    async def fake_find_user(_conn, *, channel, external_id):
        return None

    monkeypatch.setattr(channel_links, "find_user", fake_find_user)
    app = create_app()
    client = TestClient(app)
    r = client.post(
        "/channels/telegram/webhook",
        json=_message_update(text="hello"),
    )
    assert r.status_code == 200
    assert "don't recognize" in fake_telegram.sent[0]["text"]


def test_webhook_onboarding_success(monkeypatch, fake_telegram):
    """Valid /start link_<token> writes channel_links + sends confirmation."""
    bound_user = uuid4()
    consumed: list[str] = []
    created_links: list[dict] = []

    async def fake_consume(_conn, *, plain, channel):
        consumed.append(plain)
        assert channel == "telegram"
        return bound_user

    async def fake_create(_conn, *, user_id, channel, external_id,
                          external_username=None, metadata=None):
        created_links.append({
            "user_id": user_id, "channel": channel,
            "external_id": external_id, "external_username": external_username,
            "metadata": metadata,
        })
        return ChannelLink(
            id=uuid4(), user_id=user_id, channel=channel,
            external_id=external_id, external_username=external_username,
            metadata=metadata or {}, created_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(
        "wolfpaw.channels.telegram.consume_link_token", fake_consume,
    )
    monkeypatch.setattr(channel_links, "create", fake_create)

    app = create_app()
    client = TestClient(app)
    r = client.post(
        "/channels/telegram/webhook",
        json=_message_update(text="/start link_my-plain-token", from_id=9999),
    )
    assert r.status_code == 200
    assert consumed == ["my-plain-token"]
    assert len(created_links) == 1
    assert created_links[0]["user_id"] == bound_user
    assert created_links[0]["external_id"] == "9999"
    assert created_links[0]["external_username"] == "alice"
    # Confirmation pushed back.
    assert any("You're linked" in s["text"] for s in fake_telegram.sent)


def test_webhook_onboarding_invalid_token_surfaces_error(monkeypatch, fake_telegram):
    from wolfpaw.channels.telegram_tokens import LinkTokenError

    async def fake_consume(_conn, *, plain, channel):
        raise LinkTokenError("token expired")

    monkeypatch.setattr(
        "wolfpaw.channels.telegram.consume_link_token", fake_consume,
    )
    app = create_app()
    client = TestClient(app)
    r = client.post(
        "/channels/telegram/webhook",
        json=_message_update(text="/start link_bad"),
    )
    assert r.status_code == 200
    assert any("didn't work" in s["text"] for s in fake_telegram.sent)


def test_webhook_linked_user_dispatches_slash_command(monkeypatch, fake_telegram):
    """A `/help` from a linked user should route through the dispatcher
    and the response should land in Telegram."""
    bound_user = uuid4()

    async def fake_find_user(_conn, *, channel, external_id):
        return bound_user

    monkeypatch.setattr(channel_links, "find_user", fake_find_user)

    app = create_app()
    client = TestClient(app)
    r = client.post(
        "/channels/telegram/webhook",
        json=_message_update(text="/help"),
    )
    assert r.status_code == 200

    # Wait briefly for the asyncio.create_task to complete.
    async def _wait():
        for _ in range(50):
            if fake_telegram.sent:
                return
            await asyncio.sleep(0.01)

    asyncio.get_event_loop().run_until_complete(_wait())
    assert len(fake_telegram.sent) >= 1
    body = fake_telegram.sent[-1]["text"]
    assert "Available commands" in body


def test_webhook_linked_user_free_form_sends_markdown_v2(monkeypatch, fake_telegram):
    """Free-form text from a linked user → Router runs → reply sent back
    via Telegram with MarkdownV2 escaping. The agent's `**bold**` is
    converted to `*bold*` and parenthesized prose is escaped."""
    bound_user = uuid4()

    async def fake_find_user(_conn, *, channel, external_id):
        return bound_user

    async def fake_resolve_thread(_conn, _user_id):
        return uuid4()

    class FakeRouter:
        async def handle(self, *, ctx, thread_id, content, emit=None):
            return "The answer is **42** (final)."

    async def fake_open_q(_conn, *, user_id):
        return None  # no pending question → free-form goes to the router

    monkeypatch.setattr(channel_links, "find_user", fake_find_user)
    monkeypatch.setattr(
        "wolfpaw.channels.telegram._resolve_telegram_thread",
        fake_resolve_thread,
    )
    monkeypatch.setattr(
        "wolfpaw.channels.telegram.pq_dao.get_open_for_user", fake_open_q,
    )
    monkeypatch.setattr(
        "wolfpaw.channels.telegram.get_router", lambda: FakeRouter(),
    )

    app = create_app()
    client = TestClient(app)
    r = client.post(
        "/channels/telegram/webhook",
        json=_message_update(text="what's the meaning of life?"),
    )
    assert r.status_code == 200

    async def _wait():
        for _ in range(50):
            if fake_telegram.sent:
                return
            await asyncio.sleep(0.01)

    asyncio.get_event_loop().run_until_complete(_wait())
    assert len(fake_telegram.sent) >= 1
    sent = fake_telegram.sent[-1]
    assert sent["parse_mode"] == "MarkdownV2"
    # **42** → *42*  and  (final). → \(final\)\.
    assert "*42*" in sent["text"]
    assert "\\(final\\)\\." in sent["text"]


def test_webhook_answers_pending_question_instead_of_new_turn(
    monkeypatch, fake_telegram,
):
    """A linked user with a task paused on `ask_user`: their next message is
    recorded as the answer (mark_answered) and the router is NOT invoked."""
    from types import SimpleNamespace

    from wolfpaw.memory.pending_questions import AnswerOutcome

    bound_user = uuid4()
    open_q = SimpleNamespace(id=uuid4())
    answered = {}
    router_called = {"hit": False}

    async def fake_find_user(_conn, *, channel, external_id):
        return bound_user

    async def fake_open_q(_conn, *, user_id):
        return open_q

    async def fake_mark_answered(_conn, *, question_id, user_id, answer):
        answered.update(question_id=question_id, user_id=user_id, answer=answer)
        return AnswerOutcome.ANSWERED

    class FakeRouter:
        async def handle(self, *, ctx, thread_id, content, emit=None):
            router_called["hit"] = True
            return "should not be called"

    monkeypatch.setattr(channel_links, "find_user", fake_find_user)
    monkeypatch.setattr(
        "wolfpaw.channels.telegram.pq_dao.get_open_for_user", fake_open_q,
    )
    monkeypatch.setattr(
        "wolfpaw.channels.telegram.pq_dao.mark_answered", fake_mark_answered,
    )
    monkeypatch.setattr(
        "wolfpaw.channels.telegram.get_router", lambda: FakeRouter(),
    )

    app = create_app()
    client = TestClient(app)
    r = client.post(
        "/channels/telegram/webhook",
        json=_message_update(text="use https://example.com"),
    )
    assert r.status_code == 200

    async def _wait():
        for _ in range(50):
            if fake_telegram.sent:
                return
            await asyncio.sleep(0.01)

    asyncio.get_event_loop().run_until_complete(_wait())
    assert answered["answer"] == "use https://example.com"
    assert answered["question_id"] == open_q.id
    assert router_called["hit"] is False
    assert "continuing" in fake_telegram.sent[-1]["text"].lower()


def test_webhook_malformed_json_doesnt_500(fake_telegram):
    app = create_app()
    client = TestClient(app)
    r = client.post(
        "/channels/telegram/webhook",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    # Returning 200 prevents Telegram from retrying garbage forever.
    assert r.status_code == 200
