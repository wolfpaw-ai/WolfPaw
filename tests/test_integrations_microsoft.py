"""Step 32 — Microsoft Calendar integration tests."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
import respx

from wolfpaw.integrations.microsoft import client as ms_client
from wolfpaw.integrations.microsoft import tools as ms_tools
from wolfpaw.memory import microsoft_links as links_dao
from wolfpaw.toolbox.registry import ToolContext, ToolError


# --- OAuth client --------------------------------------------------------


async def test_build_authorize_url_includes_scope_and_state():
    url = ms_client.build_authorize_url(
        tenant="common", client_id="abc",
        redirect_uri="https://x/cb", state="STATE",
    )
    assert "login.microsoftonline.com/common/oauth2/v2.0/authorize" in url
    assert "client_id=abc" in url
    assert "Calendars.ReadWrite" in url
    assert "offline_access" in url
    assert "state=STATE" in url


@respx.mock
async def test_exchange_code_returns_token_bundle():
    respx.post(
        "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "AT",
                "refresh_token": "RT",
                "expires_in": 3600,
                "scope": "Calendars.ReadWrite offline_access User.Read",
            },
        )
    )
    result = await ms_client.exchange_code(
        code="C", redirect_uri="x", tenant="common",
        client_id="c", client_secret="s",
    )
    assert result.access_token == "AT"
    assert result.refresh_token == "RT"
    assert "Calendars.ReadWrite" in result.scope


@respx.mock
async def test_refresh_carries_old_refresh_when_unrotated():
    respx.post(
        "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "NEW_AT", "expires_in": 3600},
        )
    )
    result = await ms_client.refresh_access_token(
        refresh_token="OLD_RT", tenant="common",
        client_id="c", client_secret="s",
    )
    assert result.access_token == "NEW_AT"
    assert result.refresh_token == "OLD_RT"


@respx.mock
async def test_exchange_code_raises_on_non_200():
    respx.post(
        "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    ).mock(return_value=httpx.Response(400, text="invalid"))
    with pytest.raises(ms_client.MicrosoftApiError):
        await ms_client.exchange_code(
            code="C", redirect_uri="x", tenant="common",
            client_id="c", client_secret="s",
        )


# --- Graph API ----------------------------------------------------------


def _link(uid: UUID) -> links_dao.MicrosoftLink:
    return links_dao.MicrosoftLink(
        user_id=uid, access_token="AT", refresh_token="RT",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        tenant_id=None, account_id=None,
        scope="Calendars.ReadWrite offline_access",
    )


@respx.mock
async def test_list_events_normalizes_graph_payload():
    uid = uuid4()
    respx.get("https://graph.microsoft.com/v1.0/me/calendarView").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "evt-1",
                        "subject": "Sync with Alice",
                        "start": {"dateTime": "2026-05-28T15:00:00",
                                  "timeZone": "UTC"},
                        "end": {"dateTime": "2026-05-28T15:30:00",
                                "timeZone": "UTC"},
                        "location": {"displayName": "Zoom"},
                        "attendees": [
                            {"emailAddress": {"address": "alice@x.com"}},
                        ],
                        "webLink": "https://outlook/foo",
                        "isAllDay": False,
                    },
                ],
            },
        )
    )
    client = ms_client.MicrosoftClient(user_id=uid, link=_link(uid))
    events = await client.list_events(
        start="2026-05-28T00:00:00", end="2026-05-29T00:00:00",
    )
    assert len(events) == 1
    e = events[0]
    assert e["id"] == "evt-1"
    assert e["subject"] == "Sync with Alice"
    assert e["start"] == "2026-05-28T15:00:00"
    assert e["location"] == "Zoom"
    assert e["attendees"] == ["alice@x.com"]


@respx.mock
async def test_create_event_sends_full_body_and_returns_normalized():
    uid = uuid4()
    captured: dict = {}

    def respond(request):
        import json as _json
        captured["body"] = _json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "id": "evt-99",
                "subject": "Dentist",
                "start": {"dateTime": "2026-05-28T15:00:00",
                          "timeZone": "America/Chicago"},
                "end": {"dateTime": "2026-05-28T15:30:00",
                        "timeZone": "America/Chicago"},
                "location": {"displayName": "Downtown clinic"},
                "attendees": [
                    {"emailAddress": {"address": "spouse@x.com"}},
                ],
                "webLink": "https://outlook/evt-99",
                "isAllDay": False,
            },
        )

    respx.post("https://graph.microsoft.com/v1.0/me/events").mock(side_effect=respond)
    client = ms_client.MicrosoftClient(user_id=uid, link=_link(uid))
    result = await client.create_event(
        subject="Dentist",
        start="2026-05-28T15:00:00", end="2026-05-28T15:30:00",
        body_html="<p>Annual checkup.</p>",
        attendees=["spouse@x.com"],
        location="Downtown clinic",
        time_zone="America/Chicago",
    )
    sent = captured["body"]
    assert sent["subject"] == "Dentist"
    assert sent["start"]["timeZone"] == "America/Chicago"
    assert sent["body"]["contentType"] == "HTML"
    assert sent["attendees"][0]["emailAddress"]["address"] == "spouse@x.com"
    assert sent["location"]["displayName"] == "Downtown clinic"
    assert result["id"] == "evt-99"
    assert result["location"] == "Downtown clinic"


# --- token refresh-on-expiry --------------------------------------------


@respx.mock
async def test_ensure_fresh_token_refreshes_expired(monkeypatch):
    uid = uuid4()
    expired = links_dao.MicrosoftLink(
        user_id=uid, access_token="OLD", refresh_token="RT",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        tenant_id=None, account_id=None, scope="x",
    )
    respx.post(
        "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "NEW_AT", "expires_in": 3600},
        )
    )
    updates: list = []

    async def fake_update(_conn, **kw):
        updates.append(kw)

    monkeypatch.setattr(
        "wolfpaw.integrations.microsoft.client.links_dao.update_tokens",
        fake_update,
    )
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_acquire():
        yield None

    monkeypatch.setattr(
        "wolfpaw.integrations.microsoft.client.acquire", fake_acquire,
    )

    client = ms_client.MicrosoftClient(user_id=uid, link=expired)
    new = await client._ensure_fresh_token()
    assert new == "NEW_AT"
    assert len(updates) == 1


# --- tools ---------------------------------------------------------------


async def test_outlook_list_events_tool(monkeypatch):
    uid = uuid4()
    captured: list = []

    class _FakeClient:
        @classmethod
        async def for_user(cls, user_id):
            return cls()

        async def list_events(self, *, start, end, max_results):
            captured.append({"start": start, "end": end,
                             "max_results": max_results})
            return [{"id": "e-1", "subject": "x"}]

    monkeypatch.setattr(
        "wolfpaw.integrations.microsoft.tools.MicrosoftClient", _FakeClient,
    )
    tool = ms_tools.OutlookListEventsTool()
    out = await tool.run(
        ToolContext(user_id=uid),
        start="2026-05-26T00:00:00", end="2026-05-27T00:00:00",
        max_results=10,
    )
    assert captured == [{
        "start": "2026-05-26T00:00:00",
        "end": "2026-05-27T00:00:00",
        "max_results": 10,
    }]
    assert out["events"][0]["id"] == "e-1"


async def test_outlook_create_event_tool(monkeypatch):
    uid = uuid4()
    captured: list = []

    class _FakeClient:
        @classmethod
        async def for_user(cls, user_id):
            return cls()

        async def create_event(self, **kw):
            captured.append(kw)
            return {"id": "evt", "subject": kw["subject"]}

    monkeypatch.setattr(
        "wolfpaw.integrations.microsoft.tools.MicrosoftClient", _FakeClient,
    )
    tool = ms_tools.OutlookCreateEventTool()
    out = await tool.run(
        ToolContext(user_id=uid),
        subject="x", start="2026-05-28T15:00:00", end="2026-05-28T15:30:00",
        attendees=["a@b.com"], time_zone="America/Chicago",
    )
    assert captured[0]["subject"] == "x"
    assert captured[0]["time_zone"] == "America/Chicago"
    assert captured[0]["attendees"] == ["a@b.com"]
    assert out["id"] == "evt"


async def test_outlook_tools_surface_not_connected(monkeypatch):
    async def for_user_raises(user_id):
        raise ms_client.MicrosoftNotConnectedError("nope")

    monkeypatch.setattr(
        "wolfpaw.integrations.microsoft.tools.MicrosoftClient.for_user",
        classmethod(lambda cls, user_id: for_user_raises(user_id)),
    )
    ctx = ToolContext(user_id=uuid4())
    with pytest.raises(ToolError, match="isn't connected"):
        await ms_tools.OutlookListEventsTool().run(
            ctx, start="2026-05-26T00:00:00", end="2026-05-27T00:00:00",
        )
    with pytest.raises(ToolError, match="isn't connected"):
        await ms_tools.OutlookCreateEventTool().run(
            ctx, subject="x", start="2026-05-26T15:00:00",
            end="2026-05-26T15:30:00",
        )


async def test_outlook_list_events_validates_inputs(monkeypatch):
    """Missing start / end → ToolError, not a Graph call."""
    monkeypatch.setattr(
        "wolfpaw.integrations.microsoft.tools.MicrosoftClient",
        type("X", (), {"for_user": classmethod(
            lambda cls, user_id: (_ for _ in ()).throw(
                AssertionError("must not be called"),
            ),
        )}),
    )
    tool = ms_tools.OutlookListEventsTool()
    with pytest.raises(ToolError, match="start"):
        await tool.run(
            ToolContext(user_id=uuid4()), start="", end="x",
        )


# --- DB-gated DAO -------------------------------------------------------


_db_required = pytest.mark.skipif(
    not os.getenv("WOLFPAW_TEST_DATABASE_URL"),
    reason="WOLFPAW_TEST_DATABASE_URL not set",
)


@pytest.fixture
async def fresh_db(monkeypatch):
    if not os.getenv("WOLFPAW_TEST_DATABASE_URL"):
        yield None
        return
    from wolfpaw.config import get_settings
    from wolfpaw.memory.db import apply_sql_file, close_pool, migrations_dir

    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    monkeypatch.setenv("WOLFPAW_DATABASE_URL", dsn)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        for f in (
            "001_init.sql", "002_auth.sql", "003_sandbox.sql",
            "004_seed_skills.sql", "005_post_evaluator.sql",
            "008_conv_compaction.sql", "009_skill_distiller.sql",
            "010_skill_supersession.sql", "011_tool_creator.sql",
            "012_integrations.sql", "013_notion.sql",
            "014_microsoft.sql",
        ):
            await apply_sql_file(conn, migrations_dir() / f)
    finally:
        await conn.close()
    await close_pool()
    yield dsn
    await close_pool()


async def _seed_user(dsn: str) -> UUID:
    conn = await asyncpg.connect(dsn=dsn)
    try:
        return await conn.fetchval(
            "INSERT INTO users (email, email_verified) VALUES ($1, TRUE)"
            " RETURNING id",
            f"ms+{uuid4().hex[:8]}@test.local",
        )
    finally:
        await conn.close()


@_db_required
async def test_microsoft_links_upsert_then_get(fresh_db):
    dsn = fresh_db
    uid = await _seed_user(dsn)
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await links_dao.upsert(
            conn, user_id=uid,
            access_token="AT", refresh_token="RT",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            tenant_id="t-1", account_id=None, scope="Calendars.ReadWrite",
        )
        got = await links_dao.get(conn, user_id=uid)
    finally:
        await conn.close()
    assert got.access_token == "AT"
    assert got.tenant_id == "t-1"
