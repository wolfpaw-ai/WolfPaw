"""Step 30 — Notion integration tests."""

from __future__ import annotations

import base64
import os
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
import respx

from wolfpaw.integrations.notion import client as notion_client
from wolfpaw.integrations.notion import tools as notion_tools
from wolfpaw.memory import notion_links as links_dao
from wolfpaw.toolbox.registry import ToolContext, ToolError


# --- OAuth client --------------------------------------------------------


async def test_build_authorize_url_includes_state_and_owner_user():
    url = notion_client.build_authorize_url(
        client_id="abc", redirect_uri="https://x/cb", state="STATE",
    )
    assert "client_id=abc" in url
    assert "response_type=code" in url
    assert "owner=user" in url
    assert "state=STATE" in url


@respx.mock
async def test_exchange_code_sends_basic_auth_and_parses_workspace():
    """Notion's token endpoint requires HTTP Basic auth on the
    client_id:client_secret pair (not in the JSON body). Verify both
    that the header is right + that the workspace fields parse."""
    captured: dict = {}

    def respond(request):
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "access_token": "secret_T",
                "workspace_id": "ws-1",
                "workspace_name": "Personal",
                "workspace_icon": "icon-url",
                "bot_id": "bot-1",
                "owner": {"type": "user", "user": {"id": "u-1"}},
            },
        )

    respx.post("https://api.notion.com/v1/oauth/token").mock(side_effect=respond)
    result = await notion_client.exchange_code(
        code="C", redirect_uri="x", client_id="cid", client_secret="csec",
    )
    expected_basic = base64.b64encode(b"cid:csec").decode("ascii")
    assert captured["authorization"] == f"Basic {expected_basic}"
    assert result.access_token == "secret_T"
    assert result.workspace_id == "ws-1"
    assert result.workspace_name == "Personal"
    assert result.bot_id == "bot-1"
    assert result.owner["type"] == "user"


@respx.mock
async def test_exchange_code_raises_on_non_200():
    respx.post("https://api.notion.com/v1/oauth/token").mock(
        return_value=httpx.Response(400, text="invalid"),
    )
    with pytest.raises(notion_client.NotionApiError):
        await notion_client.exchange_code(
            code="C", redirect_uri="x", client_id="c", client_secret="s",
        )


# --- NotionClient API ----------------------------------------------------


def _link(user_id: UUID) -> links_dao.NotionLink:
    return links_dao.NotionLink(
        user_id=user_id, access_token="secret_T",
        workspace_id="ws-1", workspace_name="Personal",
        workspace_icon=None, bot_id="bot-1",
    )


@respx.mock
async def test_notion_client_search_normalizes_results():
    uid = uuid4()
    respx.post("https://api.notion.com/v1/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "page-1",
                        "object": "page",
                        "url": "https://notion.so/page-1",
                        "last_edited_time": "2026-05-01T00:00:00Z",
                        "properties": {
                            "Name": {
                                "type": "title",
                                "title": [{"plain_text": "Q3 plan"}],
                            },
                        },
                    },
                ],
            },
        )
    )
    client = notion_client.NotionClient(user_id=uid, link=_link(uid))
    hits = await client.search("Q3")
    assert hits[0]["id"] == "page-1"
    assert hits[0]["title"] == "Q3 plan"
    assert hits[0]["url"] == "https://notion.so/page-1"


@respx.mock
async def test_notion_client_read_page_merges_page_and_blocks():
    """`read_page` makes TWO API calls (page metadata + child blocks)
    and stitches them together — verify both fire and the response is
    composed."""
    uid = uuid4()
    respx.get("https://api.notion.com/v1/pages/page-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "page-1",
                "url": "https://notion.so/page-1",
                "properties": {
                    "Name": {
                        "type": "title",
                        "title": [{"plain_text": "Q3 plan"}],
                    },
                },
            },
        )
    )
    respx.get("https://api.notion.com/v1/blocks/page-1/children").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [
                                {"plain_text": "Hello "},
                                {"plain_text": "world."},
                            ],
                        },
                    },
                ],
            },
        )
    )
    client = notion_client.NotionClient(user_id=uid, link=_link(uid))
    page = await client.read_page("page-1")
    assert page["title"] == "Q3 plan"
    assert page["blocks"][0]["text"] == "Hello world."
    assert page["blocks"][0]["type"] == "paragraph"


@respx.mock
async def test_notion_client_create_page_posts_title_and_optional_body():
    uid = uuid4()
    captured: dict = {}

    def respond(request):
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "new-page-1",
                "url": "https://notion.so/new-page-1",
            },
        )

    respx.post("https://api.notion.com/v1/pages").mock(side_effect=respond)
    client = notion_client.NotionClient(user_id=uid, link=_link(uid))
    result = await client.create_page(
        parent_page_id="parent-1", title="My new page",
        body_markdown="Some content.",
    )
    assert result["page_id"] == "new-page-1"
    sent = captured["body"]
    assert sent["parent"]["page_id"] == "parent-1"
    assert sent["properties"]["title"]["title"][0]["text"]["content"] == "My new page"
    # Body markdown becomes a paragraph block.
    assert sent["children"][0]["type"] == "paragraph"


# --- rate limit ---------------------------------------------------------


async def test_rate_limit_holds_to_3_per_second(monkeypatch):
    """Four calls in a row: the 4th must wait so the burst doesn't
    exceed 3 in any one-second window."""
    import asyncio
    import time
    from wolfpaw.integrations.notion import client as nc_mod

    nc_mod._rate_window.clear()
    started = time.monotonic()
    await nc_mod._rate_limit()
    await nc_mod._rate_limit()
    await nc_mod._rate_limit()
    await nc_mod._rate_limit()
    elapsed = time.monotonic() - started
    # 4th call should have slept until roughly 1.0s after the 1st.
    assert elapsed >= 0.9, f"rate limiter didn't enforce (elapsed={elapsed:.2f})"


# --- tools --------------------------------------------------------------


async def test_notion_search_tool_dispatches_to_client(monkeypatch):
    uid = uuid4()
    captured: list = []

    class _FakeClient:
        @classmethod
        async def for_user(cls, user_id):
            return cls()

        async def search(self, query, *, page_size):
            captured.append({"query": query, "page_size": page_size})
            return [{"id": "p-1", "title": "Q3", "url": "u",
                     "last_edited_time": "t", "object": "page"}]

    monkeypatch.setattr(
        "wolfpaw.integrations.notion.tools.NotionClient", _FakeClient,
    )
    tool = notion_tools.NotionSearchTool()
    out = await tool.run(ToolContext(user_id=uid), query="Q3", page_size=5)
    assert captured == [{"query": "Q3", "page_size": 5}]
    assert out["results"][0]["id"] == "p-1"


async def test_notion_read_page_tool_returns_page_payload(monkeypatch):
    uid = uuid4()

    class _FakeClient:
        @classmethod
        async def for_user(cls, user_id):
            return cls()

        async def read_page(self, page_id):
            return {"page_id": page_id, "title": "T", "url": "u",
                    "properties": {}, "blocks": []}

    monkeypatch.setattr(
        "wolfpaw.integrations.notion.tools.NotionClient", _FakeClient,
    )
    tool = notion_tools.NotionReadPageTool()
    out = await tool.run(ToolContext(user_id=uid), page_id="page-1")
    assert out["page_id"] == "page-1"


async def test_notion_create_page_tool_passes_through_inputs(monkeypatch):
    uid = uuid4()
    captured: list = []

    class _FakeClient:
        @classmethod
        async def for_user(cls, user_id):
            return cls()

        async def create_page(self, *, parent_page_id, title, body_markdown):
            captured.append({
                "parent_page_id": parent_page_id, "title": title,
                "body_markdown": body_markdown,
            })
            return {"page_id": "new", "url": "u", "title": title}

    monkeypatch.setattr(
        "wolfpaw.integrations.notion.tools.NotionClient", _FakeClient,
    )
    tool = notion_tools.NotionCreatePageTool()
    out = await tool.run(
        ToolContext(user_id=uid),
        parent_page_id="parent-1", title="New page",
        body_markdown="Body text.",
    )
    assert captured[0]["title"] == "New page"
    assert captured[0]["body_markdown"] == "Body text."
    assert out["page_id"] == "new"


async def test_notion_tools_surface_not_connected_message(monkeypatch):
    async def for_user_raises(user_id):
        raise notion_client.NotionNotConnectedError("nope")

    monkeypatch.setattr(
        "wolfpaw.integrations.notion.tools.NotionClient.for_user",
        classmethod(lambda cls, user_id: for_user_raises(user_id)),
    )
    ctx = ToolContext(user_id=uuid4())
    with pytest.raises(ToolError, match="isn't connected"):
        await notion_tools.NotionSearchTool().run(ctx, query="x")
    with pytest.raises(ToolError, match="isn't connected"):
        await notion_tools.NotionReadPageTool().run(ctx, page_id="p")
    with pytest.raises(ToolError, match="isn't connected"):
        await notion_tools.NotionCreatePageTool().run(
            ctx, parent_page_id="p", title="t",
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
            f"nt+{uuid4().hex[:8]}@test.local",
        )
    finally:
        await conn.close()


@_db_required
async def test_notion_links_upsert_then_get(fresh_db):
    dsn = fresh_db
    uid = await _seed_user(dsn)
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await links_dao.upsert(
            conn, user_id=uid, access_token="secret_T",
            workspace_id="ws-1", workspace_name="Personal",
            workspace_icon=None, bot_id="bot-1",
            owner={"type": "user", "user": {"id": "u-1"}},
        )
        got = await links_dao.get(conn, user_id=uid)
    finally:
        await conn.close()
    assert got is not None
    assert got.access_token == "secret_T"
    assert got.workspace_name == "Personal"
    assert got.owner["type"] == "user"


@_db_required
async def test_notion_links_upsert_overwrites(fresh_db):
    dsn = fresh_db
    uid = await _seed_user(dsn)
    conn = await asyncpg.connect(dsn=dsn)
    try:
        for ws in ("ws-1", "ws-2", "ws-3"):
            await links_dao.upsert(
                conn, user_id=uid, access_token=f"T-{ws}",
                workspace_id=ws, workspace_name=ws,
                workspace_icon=None, bot_id="b",
                owner={},
            )
        got = await links_dao.get(conn, user_id=uid)
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM notion_links WHERE user_id = $1", uid,
        )
    finally:
        await conn.close()
    assert got.workspace_id == "ws-3"
    assert count == 1
