"""The Quick agent offers an integration's tools only to connected users."""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from wolfpaw.agents import quick
from wolfpaw.integrations import dmitris_fabulous  # noqa: F401 — registers df_*


@pytest.fixture
def connected(monkeypatch):
    names: list[str] = []

    @asynccontextmanager
    async def fake_acquire():
        yield object()

    async def fake_connected(conn, *, user_id):
        return list(names)

    integration = quick.integrations_base.get_integration("dmitris_fabulous")
    monkeypatch.setattr(quick, "acquire", fake_acquire)
    monkeypatch.setattr(quick.integrations_base, "configured_integrations", lambda: [integration])
    monkeypatch.setattr(quick.integrations_base, "connected_framework_integrations", fake_connected)
    return names


async def test_connected_user_gets_df_tools_and_hint(connected):
    connected.append("dmitris_fabulous")
    tools, hints = await quick.QuickAgent()._integration_tools(uuid4())
    assert "df_search" in {t.name for t in tools}
    assert all(t.name.startswith("df_") for t in tools)
    assert "df_search" in hints[0]


async def test_unconnected_user_gets_nothing(connected):
    assert await quick.QuickAgent()._integration_tools(uuid4()) == ([], [])


async def test_df_tools_are_refused_without_a_connection():
    result = await quick.QuickAgent()._run_tool(None, "df_search", {}, "u1")
    assert result["is_error"] is True and "not available" in result["content"]
