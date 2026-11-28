"""Integration framework: registry, standard routes, planner hook, log redaction."""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wolfpaw.auth.deps import require_user_id
from wolfpaw.integrations import base
from wolfpaw.integrations.base import (
    ConnectError, Credentials, Integration, register_integration,
)
from wolfpaw.memory import integration_links as links_dao
from wolfpaw.tracing import redact_secrets, _redact_processor


class _FakeIntegration(Integration):
    name = "fake_service"
    display_name = "Fake Service"
    auth_kind = "api_key"
    tool_prefix = "fake"

    def __init__(self, configured=True):
        self._configured = configured

    def is_configured(self):
        return self._configured

    async def connect_api_key(self, api_key):
        if api_key == "bad":
            raise ConnectError("rejected")
        return Credentials(
            credentials={"api_key": api_key}, scopes=["read"], display={"key_prefix": api_key[:4]},
        )


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(base, "_integrations", {})
    return base._integrations


class _MemoryLinks:
    """In-memory stand-in for the integration_links DAO."""

    def __init__(self):
        self.rows = {}

    async def upsert(self, conn, *, user_id, provider, auth_kind, credentials, scopes=None, display=None, expires_at=None):
        from datetime import datetime, timezone
        self.rows[(user_id, provider)] = links_dao.IntegrationLink(
            user_id=user_id, provider=provider, auth_kind=auth_kind,
            credentials=credentials, scopes=list(scopes or []), display=dict(display or {}),
            created_at=datetime.now(timezone.utc),
        )

    async def get(self, conn, *, user_id, provider):
        return self.rows.get((user_id, provider))

    async def delete(self, conn, *, user_id, provider):
        return self.rows.pop((user_id, provider), None) is not None


@pytest.fixture
def api(monkeypatch, registry):
    user_id = uuid4()
    store = _MemoryLinks()
    monkeypatch.setattr(base, "links_dao", store)

    @asynccontextmanager
    async def fake_acquire():
        yield object()

    monkeypatch.setattr(base, "acquire", fake_acquire)
    app = FastAPI()
    app.include_router(base.build_router())
    app.dependency_overrides[require_user_id] = lambda: user_id
    return TestClient(app), user_id, store


# --- registry ------------------------------------------------------------


def test_register_requires_name_and_prefix(registry):
    class NoPrefix(Integration):
        name = "x"
    with pytest.raises(ValueError):
        register_integration(NoPrefix())
    class NoName(Integration):
        tool_prefix = "x"
    with pytest.raises(ValueError):
        register_integration(NoName())


def test_unconfigured_integrations_are_hidden(registry):
    register_integration(_FakeIntegration(configured=False))
    assert base.get_integration("fake_service") is not None
    assert base.configured_integrations() == []


def test_load_entry_points_skips_a_broken_plugin(monkeypatch, registry):
    class _EP:
        def __init__(self, name, fail):
            self.name, self._fail = name, fail
        def load(self):
            if self._fail:
                raise RuntimeError("boom")

    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda group: [_EP("good", False), _EP("bad", True)],
    )
    assert base.load_entry_points() == ["good"]


async def test_connected_framework_integrations_lists_only_linked(monkeypatch, registry):
    register_integration(_FakeIntegration())
    uid = uuid4()
    store = _MemoryLinks()
    monkeypatch.setattr(base, "links_dao", store)
    assert await base.connected_framework_integrations(object(), user_id=uid) == []
    await store.upsert(None, user_id=uid, provider="fake_service", auth_kind="api_key", credentials={"api_key": "k"})
    assert await base.connected_framework_integrations(object(), user_id=uid) == ["fake_service"]


# --- standard routes -----------------------------------------------------


def test_list_connect_status_disconnect_roundtrip(api):
    client, user_id, store = api
    register_integration(_FakeIntegration())

    cards = client.get("/integrations").json()["integrations"]
    assert [c["name"] for c in cards] == ["fake_service"]
    assert cards[0]["connected"] is False

    r = client.post("/integrations/fake_service/connect", json={"api_key": " secret-key "})
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is True and body["display"] == {"key_prefix": "secr"}
    # The credential is stored, but never returned by any route.
    assert store.rows[(user_id, "fake_service")].credentials == {"api_key": "secret-key"}
    assert "secret-key" not in r.text
    assert "secret-key" not in client.get("/integrations").text

    assert client.get("/integrations/fake_service").json()["connected"] is True
    assert client.delete("/integrations/fake_service").json() == {"disconnected": True}
    assert client.get("/integrations/fake_service").json()["connected"] is False


def test_connect_failure_returns_400_without_storing(api):
    client, user_id, store = api
    register_integration(_FakeIntegration())
    r = client.post("/integrations/fake_service/connect", json={"api_key": "bad"})
    assert r.status_code == 400 and r.json()["detail"] == "rejected"
    assert store.rows == {}


def test_blank_key_and_unknown_or_unconfigured_integration(api):
    client, _, _ = api
    register_integration(_FakeIntegration())
    register_integration(type("Hidden", (_FakeIntegration,), {"name": "hidden"})(configured=False))
    assert client.post("/integrations/fake_service/connect", json={"api_key": "  "}).status_code == 400
    assert client.get("/integrations/nope").status_code == 404
    assert client.get("/integrations/hidden").status_code == 404
    assert client.post("/integrations/hidden/connect", json={"api_key": "k"}).status_code == 404


def test_oauth_integrations_reject_pasted_keys(api):
    client, _, _ = api
    class OAuthOne(_FakeIntegration):
        name = "oauthy"
        auth_kind = "oauth"
    register_integration(OAuthOne())
    assert client.post("/integrations/oauthy/connect", json={"api_key": "k"}).status_code == 400


# --- planner hook --------------------------------------------------------


def test_planner_block_names_framework_prefix_and_hint(registry):
    from wolfpaw.agents import planner

    class Hinted(_FakeIntegration):
        def planner_hint(self):
            return "Use fake_* for fake things."

    register_integration(Hinted())
    block = planner._format_context_block(
        past_plans=[], relevant_skills=[], summaries=[], vector_recall=[],
        user_tools=[], connected_integrations=["fake_service"],
    )
    assert "`fake_service` connected — `fake_*` tools are usable." in block
    assert "Use fake_* for fake things." in block


def test_planner_block_lists_framework_prefixes_when_none_connected(registry):
    from wolfpaw.agents import planner

    register_integration(_FakeIntegration())
    block = planner._format_context_block(
        past_plans=[], relevant_skills=[], summaries=[], vector_recall=[],
        user_tools=[], connected_integrations=[],
    )
    assert "`fake_*`" in block and "`dropbox_*`" in block


# --- log redaction -------------------------------------------------------


def test_redact_secrets_scrubs_api_keys():
    key = "df_test_" + "A" * 43
    token = "dfa_live_" + "b" * 43
    assert key not in redact_secrets(f"failed with {key} here")
    assert token not in redact_secrets(f"Bearer {token}")
    assert redact_secrets("plain text, df_notakey") == "plain text, df_notakey"


def test_log_processor_redacts_nested_values():
    key = "df_live_" + "Z" * 43
    out = _redact_processor(None, "info", {"event": "x", "detail": {"hdr": [f"Bearer {key}"]}, "n": 3})
    assert key not in str(out)
    assert out["n"] == 3
