"""http_get unit tests using `respx` to mock httpx."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
import respx

from wolfpaw.toolbox.registry import ToolContext, ToolError, get_registry


def _ctx():
    return ToolContext(user_id=uuid4())


@respx.mock
async def test_extracts_text_from_html():
    respx.get("https://example.com/article").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=(
                "<html><body><article>"
                "<h1>Big Headline</h1>"
                "<p>The body content of the article goes here.</p>"
                "</article></body></html>"
            ),
        )
    )
    tool = get_registry().get("http_get")
    out = await tool.run(_ctx(), url="https://example.com/article")
    assert out["status_code"] == 200
    assert "body content" in out["text"]
    assert out["truncated"] is False


@respx.mock
async def test_returns_plaintext_verbatim_for_non_html():
    respx.get("https://example.com/raw.txt").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text="line one\nline two",
        )
    )
    tool = get_registry().get("http_get")
    out = await tool.run(_ctx(), url="https://example.com/raw.txt")
    assert out["text"] == "line one\nline two"


@respx.mock
async def test_truncates_to_cap(monkeypatch):
    monkeypatch.setenv("WOLFPAW_HTTP_GET_MAX_BYTES", "16")
    from wolfpaw.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]

    respx.get("https://example.com/big.txt").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text="x" * 50,
        )
    )
    tool = get_registry().get("http_get")
    out = await tool.run(_ctx(), url="https://example.com/big.txt")
    assert out["truncated"] is True
    assert len(out["text"]) == 16

    get_settings.cache_clear()  # type: ignore[attr-defined]


async def test_rejects_non_http_url():
    tool = get_registry().get("http_get")
    with pytest.raises(ToolError):
        await tool.run(_ctx(), url="ftp://example.com/")
    with pytest.raises(ToolError):
        await tool.run(_ctx(), url="/local/path")


@respx.mock
async def test_network_error_becomes_tool_error():
    respx.get("https://example.com/down").mock(
        side_effect=httpx.ConnectError("boom")
    )
    tool = get_registry().get("http_get")
    with pytest.raises(ToolError, match="http error"):
        await tool.run(_ctx(), url="https://example.com/down")
