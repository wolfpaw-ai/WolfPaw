"""Three Notion tools (step 30)."""

from __future__ import annotations

from typing import Any

from wolfpaw.integrations.notion.client import (
    NotionApiError, NotionClient, NotionNotConnectedError,
)
from wolfpaw.toolbox.registry import (
    Tool, ToolContext, ToolError, register_tool,
)


_NOT_CONNECTED_MSG = (
    "Notion isn't connected for this user. Have them visit"
    " /integrations to connect their Notion workspace, then retry."
)


@register_tool
class NotionSearchTool(Tool):
    name = "notion_search"
    description = (
        "Search pages in the user's Notion workspace that the Wolfpaw"
        " integration has access to. Returns up to `page_size` hits"
        " (default 10) with each page's id, title, url, and last-edited"
        " time."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search text. Notion's search is fuzzy.",
            },
            "page_size": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "default": 10,
            },
        },
        "required": ["query"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        query = inputs.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolError("`query` must be a non-empty string")
        page_size = int(inputs.get("page_size", 10))
        try:
            client = await NotionClient.for_user(ctx.user_id)
        except NotionNotConnectedError as e:
            raise ToolError(_NOT_CONNECTED_MSG) from e
        try:
            hits = await client.search(query, page_size=page_size)
        except NotionApiError as e:
            raise ToolError(f"Notion search failed: {e}") from e
        return {"results": hits, "query": query}


@register_tool
class NotionReadPageTool(Tool):
    name = "notion_read_page"
    description = (
        "Fetch the title + top-level block contents of a Notion page"
        " by id. The id comes from a prior `notion_search` call."
        " Nested blocks aren't recursively expanded; re-call with the"
        " sub-block id to drill in."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "page_id": {
                "type": "string",
                "description": "Notion page id (UUID with or without dashes).",
            },
        },
        "required": ["page_id"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        page_id = inputs.get("page_id")
        if not isinstance(page_id, str) or not page_id.strip():
            raise ToolError("`page_id` must be a non-empty string")
        try:
            client = await NotionClient.for_user(ctx.user_id)
        except NotionNotConnectedError as e:
            raise ToolError(_NOT_CONNECTED_MSG) from e
        try:
            return await client.read_page(page_id)
        except NotionApiError as e:
            raise ToolError(f"Notion read_page failed: {e}") from e


@register_tool
class NotionCreatePageTool(Tool):
    name = "notion_create_page"
    description = (
        "Create a new Notion page under an existing parent page. The"
        " parent must already be shared with the Wolfpaw integration."
        " `body_markdown` (optional) becomes a single paragraph block;"
        " richer block structures are out of scope for v1."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "parent_page_id": {
                "type": "string",
                "description": (
                    "Notion page id under which the new page is"
                    " created. Find it via `notion_search`."
                ),
            },
            "title": {
                "type": "string",
                "description": "Title of the new page.",
            },
            "body_markdown": {
                "type": "string",
                "description": (
                    "Optional body, rendered as a single paragraph"
                    " block."
                ),
            },
        },
        "required": ["parent_page_id", "title"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        parent_page_id = inputs.get("parent_page_id")
        title = inputs.get("title")
        body_markdown = inputs.get("body_markdown")
        if not isinstance(parent_page_id, str) or not parent_page_id.strip():
            raise ToolError("`parent_page_id` must be a non-empty string")
        if not isinstance(title, str) or not title.strip():
            raise ToolError("`title` must be a non-empty string")
        try:
            client = await NotionClient.for_user(ctx.user_id)
        except NotionNotConnectedError as e:
            raise ToolError(_NOT_CONNECTED_MSG) from e
        try:
            return await client.create_page(
                parent_page_id=parent_page_id,
                title=title,
                body_markdown=body_markdown,
            )
        except NotionApiError as e:
            raise ToolError(f"Notion create_page failed: {e}") from e
