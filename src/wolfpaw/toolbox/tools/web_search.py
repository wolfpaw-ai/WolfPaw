"""`web_search` — LLM-friendly ranked web search via Tavily.

The Tavily key is read from settings at call time (not at import) so a
deployment without a key still gets a usable error rather than a startup
crash. A mockable `_post_search` indirection makes the integration easy
to test without hitting the real API.
"""

from __future__ import annotations

from typing import Any

import httpx

from wolfpaw.config import get_settings
from wolfpaw.toolbox.registry import (
    Tool,
    ToolContext,
    ToolError,
    register_tool,
)

TAVILY_URL = "https://api.tavily.com/search"


async def _post_search(api_key: str, query: str, max_results: int) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            TAVILY_URL,
            json={
                "api_key": api_key,
                "query": query,
                "search_depth": "basic",
                "max_results": max_results,
            },
        )
        resp.raise_for_status()
        return resp.json()


@register_tool
class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Search the web via Tavily and return a ranked list of result"
        " URLs + short snippets. Good for grounding facts and finding"
        " primary sources to follow up with `http_get`."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "default": 5,
            },
        },
        "required": ["query"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        query = inputs.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolError("`query` must be a non-empty string")
        max_results = int(inputs.get("max_results") or 5)
        max_results = max(1, min(10, max_results))

        api_key = get_settings().tavily_api_key
        if not api_key:
            raise ToolError(
                "web_search is not configured: WOLFPAW_TAVILY_API_KEY is unset"
            )
        try:
            data = await _post_search(api_key, query, max_results)
        except httpx.HTTPError as e:
            raise ToolError(f"tavily request failed: {e}") from e

        results = [
            {
                "title": r.get("title"),
                "url": r.get("url"),
                "content": r.get("content"),
                "score": r.get("score"),
            }
            for r in (data.get("results") or [])
        ]
        return {"query": query, "results": results}
