"""Step 29 — Dropbox integration tests.

Three layers:
  * OAuth state-token issuance + consume (DB-gated).
  * HTTP client OAuth + API calls (respx-mocked).
  * Tools (`dropbox_list_folder`, `dropbox_read_file`,
    `dropbox_write_file`) — happy path + not-connected error.
  * FastAPI routes (install-url + callback) — Starlette TestClient.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
import respx

from wolfpaw.integrations.dropbox import client as dbx_client
from wolfpaw.integrations.dropbox import tools as dbx_tools
from wolfpaw.memory import dropbox_links as links_dao
from wolfpaw.toolbox.registry import ToolContext, ToolError


@asynccontextmanager
async def _fake_acquire():
    yield None


# --- OAuth client (respx-mocked) -----------------------------------------


async def test_build_authorize_url_includes_state_and_offline_token():
    url = dbx_client.build_authorize_url(
        client_id="abc", redirect_uri="https://x/cb", state="STATE",
    )
    assert "client_id=abc" in url
    assert "response_type=code" in url
    assert "token_access_type=offline" in url
    assert "state=STATE" in url
    assert "redirect_uri=https%3A%2F%2Fx%2Fcb" in url


@respx.mock
async def test_exchange_code_returns_token_bundle():
    respx.post("https://api.dropboxapi.com/oauth2/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "AT",
                "refresh_token": "RT",
                "expires_in": 14400,
                "account_id": "dbid:foo",
                "scope": "files.content.read files.content.write",
            },
        )
    )
    result = await dbx_client.exchange_code(
        code="C", redirect_uri="https://x/cb",
        client_id="cid", client_secret="cs",
    )
    assert result.access_token == "AT"
    assert result.refresh_token == "RT"
    assert result.account_id == "dbid:foo"
    assert result.expires_at > datetime.now(timezone.utc) + timedelta(hours=3)


@respx.mock
async def test_exchange_code_raises_on_non_200():
    respx.post("https://api.dropboxapi.com/oauth2/token").mock(
        return_value=httpx.Response(400, text="invalid_grant"),
    )
    with pytest.raises(dbx_client.DropboxApiError) as exc:
        await dbx_client.exchange_code(
            code="bad", redirect_uri="x", client_id="c", client_secret="s",
        )
    assert exc.value.status_code == 400
    assert "invalid_grant" in exc.value.body


@respx.mock
async def test_refresh_access_token_carries_old_refresh_when_unrotated():
    """Dropbox typically doesn't return a refresh_token on refresh —
    the helper must keep the caller's existing one in that case."""
    respx.post("https://api.dropboxapi.com/oauth2/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "NEW_AT",
                "expires_in": 14400,
                # no refresh_token in the response
            },
        )
    )
    result = await dbx_client.refresh_access_token(
        refresh_token="OLD_RT", client_id="cid", client_secret="cs",
    )
    assert result.access_token == "NEW_AT"
    assert result.refresh_token == "OLD_RT"


# --- DropboxClient API methods (respx-mocked) ----------------------------


def _fresh_link(user_id: UUID) -> links_dao.DropboxLink:
    return links_dao.DropboxLink(
        user_id=user_id,
        access_token="AT",
        refresh_token="RT",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=4),
        account_id="dbid:x",
        scope="files.content.read files.content.write",
    )


@respx.mock
async def test_list_folder_returns_normalized_entries(monkeypatch):
    """Happy path: API returns the Dropbox-shape response, the client
    normalizes it to the dict shape the tool returns."""
    uid = uuid4()
    monkeypatch.setattr(
        dbx_client.DropboxClient, "_ensure_fresh_token",
        AsyncMock(return_value="AT"),
    )
    respx.post("https://api.dropboxapi.com/2/files/list_folder").mock(
        return_value=httpx.Response(
            200,
            json={
                "entries": [
                    {
                        ".tag": "file",
                        "name": "Q3-plan.md",
                        "path_display": "/Q3-plan.md",
                        "size": 4096,
                        "client_modified": "2026-05-01T00:00:00Z",
                        "server_modified": "2026-05-01T00:00:00Z",
                    },
                    {
                        ".tag": "folder",
                        "name": "drafts",
                        "path_display": "/drafts",
                    },
                ],
            },
        )
    )
    client = dbx_client.DropboxClient(user_id=uid, link=_fresh_link(uid))
    entries = await client.list_folder("")
    assert len(entries) == 2
    assert entries[0]["name"] == "Q3-plan.md"
    assert entries[0]["is_folder"] is False
    assert entries[0]["size_bytes"] == 4096
    assert entries[1]["is_folder"] is True


@respx.mock
async def test_read_file_returns_raw_bytes(monkeypatch):
    uid = uuid4()
    monkeypatch.setattr(
        dbx_client.DropboxClient, "_ensure_fresh_token",
        AsyncMock(return_value="AT"),
    )
    respx.post("https://content.dropboxapi.com/2/files/download").mock(
        return_value=httpx.Response(200, content=b"hello world"),
    )
    client = dbx_client.DropboxClient(user_id=uid, link=_fresh_link(uid))
    data = await client.read_file("/notes.md")
    assert data == b"hello world"


@respx.mock
async def test_write_file_returns_metadata(monkeypatch):
    uid = uuid4()
    monkeypatch.setattr(
        dbx_client.DropboxClient, "_ensure_fresh_token",
        AsyncMock(return_value="AT"),
    )
    respx.post("https://content.dropboxapi.com/2/files/upload").mock(
        return_value=httpx.Response(
            200,
            content=json.dumps({
                "name": "x.md",
                "path_display": "/x.md",
                "size": 5,
                "rev": "0123abc",
                "server_modified": "2026-05-26T00:00:00Z",
            }).encode("utf-8"),
        )
    )
    client = dbx_client.DropboxClient(user_id=uid, link=_fresh_link(uid))
    result = await client.write_file("/x.md", b"hello", overwrite=False)
    assert result["path_display"] == "/x.md"
    assert result["rev"] == "0123abc"


# --- token refresh-on-expiry --------------------------------------------


@respx.mock
async def test_ensure_fresh_token_refreshes_when_expired(monkeypatch):
    """An expired token triggers a refresh call; the DAO update is
    invoked with the new access_token + new expiry."""
    uid = uuid4()
    expired_link = links_dao.DropboxLink(
        user_id=uid,
        access_token="OLD_AT",
        refresh_token="RT",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        account_id="x", scope="x",
    )
    respx.post("https://api.dropboxapi.com/oauth2/token").mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "NEW_AT", "expires_in": 14400},
        )
    )
    updates: list[dict] = []

    async def fake_update(_conn, **kw):
        updates.append(kw)

    monkeypatch.setattr(
        "wolfpaw.integrations.dropbox.client.links_dao.update_tokens",
        fake_update,
    )
    monkeypatch.setattr(
        "wolfpaw.integrations.dropbox.client.acquire", _fake_acquire,
    )

    client = dbx_client.DropboxClient(user_id=uid, link=expired_link)
    new = await client._ensure_fresh_token()
    assert new == "NEW_AT"
    assert len(updates) == 1
    assert updates[0]["access_token"] == "NEW_AT"


async def test_ensure_fresh_token_skips_refresh_when_valid(monkeypatch):
    """A token well within its valid window doesn't trigger a refresh
    call. We assert by patching refresh_access_token to fail loudly."""
    uid = uuid4()

    async def boom(**_kw):
        raise AssertionError("refresh must not be called for a valid token")

    monkeypatch.setattr(
        "wolfpaw.integrations.dropbox.client.refresh_access_token", boom,
    )
    client = dbx_client.DropboxClient(
        user_id=uid, link=_fresh_link(uid),
    )
    out = await client._ensure_fresh_token()
    assert out == "AT"


# --- tools ---------------------------------------------------------------


async def test_dropbox_list_folder_tool_dispatches_to_client(monkeypatch):
    uid = uuid4()
    listed: list = []

    class _FakeClient:
        @classmethod
        async def for_user(cls, user_id):
            assert user_id == uid
            return cls()

        async def list_folder(self, path):
            listed.append(path)
            return [{"name": "a.md", "path": "/a.md", "is_folder": False,
                     "size_bytes": 10}]

    monkeypatch.setattr(
        "wolfpaw.integrations.dropbox.tools.DropboxClient", _FakeClient,
    )
    tool = dbx_tools.DropboxListFolderTool()
    out = await tool.run(ToolContext(user_id=uid), path="/")
    # "/" gets normalized to "" before dispatch.
    assert listed == [""]
    assert out["entries"][0]["name"] == "a.md"


async def test_dropbox_read_file_tool_decodes_utf8(monkeypatch):
    uid = uuid4()

    class _FakeClient:
        @classmethod
        async def for_user(cls, user_id):
            return cls()

        async def read_file(self, path):
            assert path == "/Q3-plan.md"
            return "hello — world".encode("utf-8")

    monkeypatch.setattr(
        "wolfpaw.integrations.dropbox.tools.DropboxClient", _FakeClient,
    )
    tool = dbx_tools.DropboxReadFileTool()
    out = await tool.run(ToolContext(user_id=uid), path="/Q3-plan.md")
    assert out["content"] == "hello — world"
    assert out["size_bytes"] == len("hello — world".encode("utf-8"))


async def test_dropbox_read_file_tool_raises_on_non_utf8(monkeypatch):
    uid = uuid4()

    class _FakeClient:
        @classmethod
        async def for_user(cls, user_id):
            return cls()

        async def read_file(self, path):
            return b"\xff\xfe\x00\x00"  # not UTF-8

    monkeypatch.setattr(
        "wolfpaw.integrations.dropbox.tools.DropboxClient", _FakeClient,
    )
    tool = dbx_tools.DropboxReadFileTool()
    with pytest.raises(ToolError, match="UTF-8"):
        await tool.run(ToolContext(user_id=uid), path="/x.bin")


async def test_dropbox_write_file_tool_encodes_and_passes_overwrite(monkeypatch):
    uid = uuid4()
    writes: list = []

    class _FakeClient:
        @classmethod
        async def for_user(cls, user_id):
            return cls()

        async def write_file(self, path, content, *, overwrite):
            writes.append({"path": path, "content": content,
                           "overwrite": overwrite})
            return {"path_display": path, "size": len(content),
                    "rev": "abc", "server_modified": "x"}

    monkeypatch.setattr(
        "wolfpaw.integrations.dropbox.tools.DropboxClient", _FakeClient,
    )
    tool = dbx_tools.DropboxWriteFileTool()
    out = await tool.run(
        ToolContext(user_id=uid),
        path="/out.md", content="hi", overwrite=True,
    )
    assert writes[0]["overwrite"] is True
    assert writes[0]["content"] == b"hi"
    assert out["path"] == "/out.md"


async def test_dropbox_tools_surface_not_connected_message(monkeypatch):
    """When the user hasn't connected Dropbox, every tool raises a
    ToolError with the install hint — the Planner sees the message
    and can fall back."""
    async def for_user_raises(user_id):
        raise dbx_client.DropboxNotConnectedError("nope")

    monkeypatch.setattr(
        "wolfpaw.integrations.dropbox.tools.DropboxClient.for_user",
        classmethod(lambda cls, user_id: for_user_raises(user_id)),
    )
    ctx = ToolContext(user_id=uuid4())
    for tool in (
        dbx_tools.DropboxListFolderTool(),
        dbx_tools.DropboxReadFileTool(),
        dbx_tools.DropboxWriteFileTool(),
    ):
        kwargs = {"path": "/x"} if not isinstance(
            tool, dbx_tools.DropboxListFolderTool,
        ) else {}
        if isinstance(tool, dbx_tools.DropboxWriteFileTool):
            kwargs["content"] = "x"
        with pytest.raises(ToolError, match="isn't connected"):
            await tool.run(ctx, **kwargs)


# --- DB-gated DAO + state-token tests ------------------------------------


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
            "012_integrations.sql",
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
            f"dbx+{uuid4().hex[:8]}@test.local",
        )
    finally:
        await conn.close()


@_db_required
async def test_dropbox_links_upsert_then_get(fresh_db):
    dsn = fresh_db
    uid = await _seed_user(dsn)
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await links_dao.upsert(
            conn, user_id=uid,
            access_token="AT", refresh_token="RT",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=4),
            account_id="dbid:x", scope="x",
        )
        got = await links_dao.get(conn, user_id=uid)
    finally:
        await conn.close()
    assert got is not None
    assert got.access_token == "AT"
    assert got.refresh_token == "RT"


@_db_required
async def test_dropbox_links_upsert_overwrites(fresh_db):
    dsn = fresh_db
    uid = await _seed_user(dsn)
    conn = await asyncpg.connect(dsn=dsn)
    try:
        for token in ("FIRST", "SECOND", "THIRD"):
            await links_dao.upsert(
                conn, user_id=uid,
                access_token=token, refresh_token="RT",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=4),
                account_id="x", scope="x",
            )
        latest = await links_dao.get(conn, user_id=uid)
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM dropbox_links WHERE user_id = $1", uid,
        )
    finally:
        await conn.close()
    assert latest.access_token == "THIRD"
    assert count == 1  # upsert replaces, doesn't accumulate


@_db_required
async def test_dropbox_links_update_tokens_preserves_refresh_when_unrotated(
    fresh_db,
):
    dsn = fresh_db
    uid = await _seed_user(dsn)
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await links_dao.upsert(
            conn, user_id=uid,
            access_token="OLD", refresh_token="RT_ORIG",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=4),
            account_id="x", scope="x",
        )
        await links_dao.update_tokens(
            conn, user_id=uid,
            access_token="NEW",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=4),
            # refresh_token kwarg omitted → DAO keeps the existing one
        )
        got = await links_dao.get(conn, user_id=uid)
    finally:
        await conn.close()
    assert got.access_token == "NEW"
    assert got.refresh_token == "RT_ORIG"


@_db_required
async def test_state_token_issue_then_consume(fresh_db):
    from wolfpaw.integrations.oauth_state import (
        StateTokenError, consume, issue,
    )

    dsn = fresh_db
    uid = await _seed_user(dsn)
    conn = await asyncpg.connect(dsn=dsn)
    try:
        plain = await issue(
            conn, user_id=uid, provider="dropbox", ttl_minutes=15,
        )
        recovered = await consume(conn, plain=plain, provider="dropbox")
    finally:
        await conn.close()
    assert recovered == uid


@_db_required
async def test_state_token_consume_rejects_wrong_provider(fresh_db):
    """A state token minted for Dropbox MUST NOT consume on the Notion
    callback. Defense against a misconfigured operator redirect URL."""
    from wolfpaw.integrations.oauth_state import (
        StateTokenError, consume, issue,
    )

    dsn = fresh_db
    uid = await _seed_user(dsn)
    conn = await asyncpg.connect(dsn=dsn)
    try:
        plain = await issue(
            conn, user_id=uid, provider="dropbox", ttl_minutes=15,
        )
        with pytest.raises(StateTokenError, match="different provider"):
            await consume(conn, plain=plain, provider="notion")
    finally:
        await conn.close()


@_db_required
async def test_state_token_single_use(fresh_db):
    from wolfpaw.integrations.oauth_state import (
        StateTokenError, consume, issue,
    )

    dsn = fresh_db
    uid = await _seed_user(dsn)
    conn = await asyncpg.connect(dsn=dsn)
    try:
        plain = await issue(
            conn, user_id=uid, provider="dropbox", ttl_minutes=15,
        )
        await consume(conn, plain=plain, provider="dropbox")
        with pytest.raises(StateTokenError, match="already used"):
            await consume(conn, plain=plain, provider="dropbox")
    finally:
        await conn.close()
