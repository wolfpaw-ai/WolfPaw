"""web_search: Tavily integration with mocked POST."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
import respx

from wolfpaw.toolbox.registry import ToolContext, ToolError, get_registry
from wolfpaw.toolbox.tools.web_search import TAVILY_URL


def _ctx():
    return ToolContext(user_id=uuid4())


@respx.mock
async def test_returns_normalized_results(monkeypatch):
    monkeypatch.setenv("WOLFPAW_TAVILY_API_KEY", "test-key")
    from wolfpaw.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]

    respx.post(TAVILY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"title": "Wolfpaw docs", "url": "https://wolfpaw.ai/docs",
                     "content": "snippet 1", "score": 0.91},
                    {"title": "Wolfpaw blog", "url": "https://wolfpaw.ai/blog",
                     "content": "snippet 2", "score": 0.74},
                ]
            },
        )
    )
    tool = get_registry().get("web_search")
    out = await tool.run(_ctx(), query="wolfpaw")
    assert out["query"] == "wolfpaw"
    assert len(out["results"]) == 2
    assert out["results"][0]["url"] == "https://wolfpaw.ai/docs"

    get_settings.cache_clear()  # type: ignore[attr-defined]


async def test_missing_api_key_is_actionable_error(monkeypatch):
    monkeypatch.delenv("WOLFPAW_TAVILY_API_KEY", raising=False)
    from wolfpaw.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]
    tool = get_registry().get("web_search")
    with pytest.raises(ToolError, match="TAVILY"):
        await tool.run(_ctx(), query="anything")
    get_settings.cache_clear()  # type: ignore[attr-defined]


@respx.mock
async def test_upstream_error_becomes_tool_error(monkeypatch):
    monkeypatch.setenv("WOLFPAW_TAVILY_API_KEY", "test-key")
    from wolfpaw.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]

    respx.post(TAVILY_URL).mock(return_value=httpx.Response(500))
    tool = get_registry().get("web_search")
    with pytest.raises(ToolError):
        await tool.run(_ctx(), query="anything")

    get_settings.cache_clear()  # type: ignore[attr-defined]


async def test_empty_query_rejected():
    tool = get_registry().get("web_search")
    with pytest.raises(ToolError):
        await tool.run(_ctx(), query="   ")
