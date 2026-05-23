"""`http_get` — fetch a URL and return readable text.

httpx for the request, readability-lxml for main-content extraction.
Body length is capped (default 2 MB) so a runaway response can't blow up
the agent's context. Non-HTML responses are returned verbatim, truncated
to the same cap.
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


def _extract_main_text(html: str) -> str:
    """Best-effort: pull the article body out of HTML. Falls back to the
    raw HTML if readability can't find a candidate (rare but possible
    on extremely unusual pages)."""
    from readability import Document  # type: ignore[import-not-found]
    from lxml import html as lxml_html  # type: ignore[import-not-found]

    try:
        doc = Document(html)
        summary_html = doc.summary(html_partial=True)
        tree = lxml_html.fromstring(summary_html)
        text = tree.text_content()
        return " ".join(text.split())
    except Exception:
        return html


@register_tool
class HttpGetTool(Tool):
    name = "http_get"
    description = (
        "Fetch a URL via HTTP GET and return its readable text. HTML pages are"
        " run through a readability extractor; non-HTML is returned verbatim."
        " Response bytes are capped — do not use this to download large files."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute http(s) URL."}
        },
        "required": ["url"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        url = inputs.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ToolError("`url` must be an absolute http(s) URL")
        settings = get_settings()
        try:
            async with httpx.AsyncClient(
                timeout=settings.http_get_timeout_seconds,
                follow_redirects=True,
                max_redirects=5,
            ) as client:
                resp = await client.get(url)
        except httpx.HTTPError as e:
            raise ToolError(f"http error: {e}") from e

        cap = settings.http_get_max_bytes
        body = resp.content[:cap]
        truncated = len(resp.content) > cap
        content_type = resp.headers.get("content-type", "")

        if "html" in content_type.lower():
            try:
                text = _extract_main_text(body.decode("utf-8", errors="replace"))
            except Exception as e:
                raise ToolError(f"could not extract text: {e}") from e
            return {
                "url": str(resp.url),
                "status_code": resp.status_code,
                "content_type": content_type,
                "text": text,
                "truncated": truncated,
            }

        return {
            "url": str(resp.url),
            "status_code": resp.status_code,
            "content_type": content_type,
            "text": body.decode("utf-8", errors="replace"),
            "truncated": truncated,
        }
