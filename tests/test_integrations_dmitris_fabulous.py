"""Document Organizer integration: connect validation, client, tools."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
import respx

from wolfpaw.config import get_settings
from wolfpaw.integrations import dmitris_fabulous as df
from wolfpaw.integrations.dmitris_fabulous import client as df_client
from wolfpaw.integrations.dmitris_fabulous import tools as df_tools
from wolfpaw.integrations.base import ConnectError
from wolfpaw.toolbox.registry import ToolContext, ToolError, get_registry

BASE = "https://dev.api.example.test"
KEY = "df_test_" + "A" * 43
ALL_SCOPES = ["documents:read", "documents:content", "search", "reports"]


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("WOLFPAW_DF_BASE_URL", BASE)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _me(scopes=None):
    return {
        "user": {"id": "usr_abc"},
        "token": {"id": "t1", "name": "Wolfpaw", "prefix": KEY[:16], "kind": "personal", "scopes": scopes or ALL_SCOPES},
        "environment": "dev",
    }


def _err(status, code, message="nope", **headers):
    return httpx.Response(
        status, headers=headers,
        json={"error": {"type": "x", "code": code, "message": message, "request_id": "req-9"}},
    )


# --- registration / tools exist -----------------------------------------


def test_integration_registers_tools_and_is_configured_by_base_url(monkeypatch):
    integ = df.DocumentOrganizerIntegration()
    assert integ.is_configured() is True
    monkeypatch.setenv("WOLFPAW_DF_BASE_URL", "")
    get_settings.cache_clear()
    assert integ.is_configured() is False
    names = get_registry().names()
    for tool in ("df_search", "df_get_document", "df_document_link", "df_list_lists", "df_report"):
        assert tool in names


# --- connect -------------------------------------------------------------


@respx.mock
async def test_connect_validates_key_and_scopes():
    route = respx.get(f"{BASE}/v1/me").mock(return_value=httpx.Response(200, json=_me()))
    creds = await df.DocumentOrganizerIntegration().connect_api_key(KEY)
    assert route.calls.last.request.headers["authorization"] == f"Bearer {KEY}"
    assert creds.credentials == {"api_key": KEY}
    assert creds.display["key_prefix"] == KEY[:16]
    assert creds.scopes == ALL_SCOPES


@respx.mock
async def test_connect_rejects_missing_scopes_bad_key_and_wrong_shape():
    integ = df.DocumentOrganizerIntegration()
    respx.get(f"{BASE}/v1/me").mock(return_value=httpx.Response(200, json=_me(["search"])))
    with pytest.raises(ConnectError, match="documents:read"):
        await integ.connect_api_key(KEY)

    respx.get(f"{BASE}/v1/me").mock(return_value=_err(401, "invalid_token"))
    with pytest.raises(ConnectError, match="rejected that key") as exc:
        await integ.connect_api_key(KEY)
    assert KEY not in str(exc.value)

    with pytest.raises(ConnectError, match="start with df_"):
        await integ.connect_api_key("sk-not-ours")


# --- client --------------------------------------------------------------


@respx.mock
async def test_client_retries_once_on_429_honoring_retry_after(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("wolfpaw.integrations.dmitris_fabulous.client.asyncio.sleep", fake_sleep)
    respx.get(f"{BASE}/v1/lists").mock(side_effect=[
        _err(429, "rate_limited", **{"Retry-After": "3"}),
        httpx.Response(200, json={"lists": [{"id": "l1", "name": "Bills"}]}),
    ])
    client = df_client.DocumentOrganizerClient(api_key=KEY)
    assert await client.lists() == [{"id": "l1", "name": "Bills"}]
    assert sleeps == [3.0]


@respx.mock
async def test_client_does_not_retry_long_agent_calls_on_5xx():
    route = respx.post(f"{BASE}/v1/search").mock(return_value=_err(502, "agent_failed", "Search failed. Try again."))
    client = df_client.DocumentOrganizerClient(api_key=KEY)
    with pytest.raises(df_client.DocumentOrganizerError) as exc:
        await client.search("x")
    assert exc.value.code == "agent_failed" and exc.value.request_id == "req-9"
    assert route.call_count == 1


@respx.mock
async def test_client_search_sends_brief_and_list(monkeypatch):
    route = respx.post(f"{BASE}/v1/search").mock(return_value=httpx.Response(200, json={"answer": "a", "count": 0, "documents": []}))
    client = df_client.DocumentOrganizerClient(api_key=KEY)
    await client.search("receipts", list_id="l1", brief=True)
    assert json.loads(route.calls.last.request.content) == {"query": "receipts", "list_id": "l1", "answer_style": "brief"}


async def test_client_without_base_url_fails_clearly(monkeypatch):
    monkeypatch.setenv("WOLFPAW_DF_BASE_URL", "")
    get_settings.cache_clear()
    with pytest.raises(df_client.DocumentOrganizerError) as exc:
        await df_client.DocumentOrganizerClient(api_key=KEY).me()
    assert exc.value.code == "not_configured"


# --- tools ---------------------------------------------------------------


class _FakeClient:
    """Stands in for DocumentOrganizerClient inside tools."""

    def __init__(self, **kw):
        self.calls = []
        self.kw = kw

    @classmethod
    async def for_user(cls, user_id):
        return _CURRENT

    async def lists(self):
        return [{"id": "l-bills", "name": "Bills"}, {"id": "l-recipes", "name": "Recipes"}]

    async def search(self, query, *, list_id=None, brief=False):
        self.calls.append(("search", query, list_id, brief))
        docs = [
            {"id": f"d{i}", "name": f"Doc {i}", "type": "receipt", "category": "groceries",
             "entity": {"name": "HEB"}, "transaction_date": "2026-10-03",
             "amounts": {"total": 10.5}, "summary": "x" * 500}
            for i in range(25)
        ]
        return {"answer": "Found them.", "count": 25, "documents": docs}

    async def get_document(self, doc_id):
        if doc_id == "missing":
            raise df_client.DocumentOrganizerError(404, "not_found", "Document not found.")
        return {"id": doc_id, "name": "Doc", "line_items": [{"description": "milk"}]}

    async def get_document_text(self, doc_id):
        return {"id": doc_id, "text": "t" * 9000}

    async def document_file(self, doc_id):
        return {"url": "https://s3/signed", "expires_at": "soon"}

    async def report(self, query, *, output_types=None, list_id=None):
        self.calls.append(("report", query, output_types, list_id))
        return {
            "report": {"text": "Total $20", "table": {"columns": ["store", "amt"], "rows": [[f"s{i}", i] for i in range(30)]}, "ids": []},
            "documents": [{"id": "d1"}],
        }

    async def export_report(self, report, *, fmt, query=None):
        self.calls.append(("export", fmt))
        return b"store,amt\r\ns0,0"


_CURRENT = None


@pytest.fixture
def fake(monkeypatch):
    global _CURRENT
    _CURRENT = _FakeClient()
    monkeypatch.setattr(df_tools, "DocumentOrganizerClient", _FakeClient)
    return _CURRENT


async def test_search_returns_capped_compact_stubs_with_untrusted_note(fake):
    out = await df_tools.DfSearchTool().run(ToolContext(user_id=uuid4()), query="groceries")
    assert out["answer"] == "Found them." and out["total_found"] == 25
    assert len(out["documents"]) == 20 and out["shown"] == 20
    stub = out["documents"][0]
    assert set(stub) == {"id", "name", "type", "category", "entity", "date", "total", "summary"}
    assert len(stub["summary"]) <= 200
    assert "untrusted" in out["note"].lower()
    assert fake.calls[0] == ("search", "groceries", None, False)


async def test_search_asks_for_brief_answers_on_voice_and_resolves_list_names(fake):
    await df_tools.DfSearchTool().run(
        ToolContext(user_id=uuid4(), channel="voice"), query="bills", list_name="bill",
    )
    assert fake.calls[0] == ("search", "bills", "l-bills", True)


async def test_search_unknown_list_names_the_real_ones(fake):
    with pytest.raises(ToolError, match="Bills, Recipes"):
        await df_tools.DfSearchTool().run(ToolContext(user_id=uuid4()), query="x", list_name="Taxes")


async def test_search_validates_input(fake):
    with pytest.raises(ToolError):
        await df_tools.DfSearchTool().run(ToolContext(user_id=uuid4()), query="  ")


async def test_get_document_truncates_text_and_labels_untrusted(fake):
    out = await df_tools.DfGetDocumentTool().run(ToolContext(user_id=uuid4()), id="d1", include_text=True)
    assert len(out["text"]) == 8000 and out["truncated"] is True
    assert out["line_items"][0]["description"] == "milk"
    assert "untrusted" in out["note"].lower()
    no_text = await df_tools.DfGetDocumentTool().run(ToolContext(user_id=uuid4()), id="d1")
    assert "text" not in no_text


async def test_get_document_not_found_is_a_clean_tool_error(fake):
    with pytest.raises(ToolError, match="No document with id 'missing'"):
        await df_tools.DfGetDocumentTool().run(ToolContext(user_id=uuid4()), id="missing")


async def test_document_link_and_list_lists(fake):
    link = await df_tools.DfDocumentLinkTool().run(ToolContext(user_id=uuid4()), id="d1")
    assert link["url"].startswith("https://")
    lists = await df_tools.DfListListsTool().run(ToolContext(user_id=uuid4()))
    assert [l["name"] for l in lists["lists"]] == ["Bills", "Recipes"]


async def test_report_saves_csv_to_workspace_and_previews_table(fake, monkeypatch):
    saved = {}

    async def fake_save(ctx, data):
        saved["data"] = data
        return {"file_id": "f1", "filename": "document-report.csv", "size_bytes": len(data), "mime_type": "text/csv"}

    monkeypatch.setattr(df_tools, "_save_csv", fake_save)
    out = await df_tools.DfReportTool().run(ToolContext(user_id=uuid4()), query="remodel spend")
    assert out["text"] == "Total $20"
    assert out["table"]["row_count"] == 30 and len(out["table"]["preview_rows"]) == 10
    assert out["file"]["filename"] == "document-report.csv"
    assert saved["data"].startswith(b"store,amt")
    assert ("export", "csv") in fake.calls


async def test_tools_translate_api_errors(monkeypatch, fake):
    async def boom(self, *a, **k):
        raise df_client.DocumentOrganizerError(401, "invalid_token", "bad")

    monkeypatch.setattr(_FakeClient, "search", boom)
    with pytest.raises(ToolError, match="rejected"):
        await df_tools.DfSearchTool().run(ToolContext(user_id=uuid4()), query="x")

    async def limited(self, *a, **k):
        raise df_client.DocumentOrganizerError(429, "rate_limited", "slow down")

    monkeypatch.setattr(_FakeClient, "search", limited)
    with pytest.raises(ToolError, match="rate limiting"):
        await df_tools.DfSearchTool().run(ToolContext(user_id=uuid4()), query="x")


async def test_not_connected_message_points_to_profile(monkeypatch):
    async def raising(cls, user_id):
        raise df_client.DocumentOrganizerNotConnectedError("nope")

    monkeypatch.setattr(df_tools.DocumentOrganizerClient, "for_user", classmethod(raising))
    with pytest.raises(ToolError, match="Profile"):
        await df_tools.DfListListsTool().run(ToolContext(user_id=uuid4()))
