"""Notion HTTP client wrapping the four API operations we use.

Two concerns:

  * **OAuth code exchange** — :func:`exchange_code` talks to
    ``api.notion.com/v1/oauth/token`` with HTTP Basic auth (the
    client_id:client_secret pair). Notion doesn't issue refresh
    tokens, so this is the only token-side call.

  * **API calls** — :class:`NotionClient` is constructed per-user.
    Search / read_page / create_page against the user's connected
    workspace. A simple in-process rate limiter caps outbound calls
    at 3 req/s per process (Notion's published limit).

The HTTP transport is injectable via :func:`set_http_client_for_test`
so unit tests drive flows without real network calls.
"""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx

from wolfpaw.memory import notion_links as links_dao
from wolfpaw.memory.db import acquire
from wolfpaw.tracing import get_logger

log = get_logger()


_AUTHORIZE_URL = "https://api.notion.com/v1/oauth/authorize"
_TOKEN_URL = "https://api.notion.com/v1/oauth/token"
_API_BASE = "https://api.notion.com/v1"
_API_VERSION = "2022-06-28"
_RATE_LIMIT_PER_SECOND = 3


_http_client: httpx.AsyncClient | None = None


def set_http_client_for_test(client: httpx.AsyncClient | None) -> None:
    global _http_client
    _http_client = client


def _client_or_new(timeout: float) -> httpx.AsyncClient:
    return _http_client or httpx.AsyncClient(timeout=timeout)


class NotionApiError(Exception):
    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"Notion API {status_code}: {body}")
        self.status_code = status_code
        self.body = body


class NotionNotConnectedError(Exception):
    """The user hasn't connected Notion yet."""


@dataclass(frozen=True)
class TokenExchangeResult:
    access_token: str
    workspace_id: str | None
    workspace_name: str | None
    workspace_icon: str | None
    bot_id: str | None
    owner: dict[str, Any] = field(default_factory=dict)


# --- OAuth helpers -------------------------------------------------------


def build_authorize_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    from urllib.parse import urlencode

    qs = urlencode({
        "client_id": client_id,
        "response_type": "code",
        "owner": "user",  # per-user workspace install
        "redirect_uri": redirect_uri,
        "state": state,
    })
    return f"{_AUTHORIZE_URL}?{qs}"


async def exchange_code(
    *, code: str, redirect_uri: str,
    client_id: str, client_secret: str,
) -> TokenExchangeResult:
    """Trade an OAuth code for the workspace-scoped bot token.

    Notion uses HTTP Basic auth on this endpoint (not in the body)
    with `{client_id}:{client_secret}` base64-encoded."""
    basic = base64.b64encode(
        f"{client_id}:{client_secret}".encode("utf-8"),
    ).decode("ascii")
    async with _client_or_new(timeout=15.0) as client:
        resp = await client.post(
            _TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/json",
                "Notion-Version": _API_VERSION,
            },
            json={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
    if resp.status_code != 200:
        raise NotionApiError(resp.status_code, resp.text)
    payload = resp.json()
    return TokenExchangeResult(
        access_token=payload["access_token"],
        workspace_id=payload.get("workspace_id"),
        workspace_name=payload.get("workspace_name"),
        workspace_icon=payload.get("workspace_icon"),
        bot_id=payload.get("bot_id"),
        owner=payload.get("owner") or {},
    )


# --- rate limiter --------------------------------------------------------


_rate_window: deque[float] = deque()
_rate_lock = asyncio.Lock()


async def _rate_limit() -> None:
    """3 req/s sliding-window limiter. Process-wide — fine for the
    workload we expect; if multi-process arq workers need to share
    the budget, the right answer is a token bucket in Redis."""
    async with _rate_lock:
        loop = asyncio.get_running_loop()
        now = loop.time()
        while _rate_window and now - _rate_window[0] >= 1.0:
            _rate_window.popleft()
        if len(_rate_window) >= _RATE_LIMIT_PER_SECOND:
            wait = 1.0 - (now - _rate_window[0])
            if wait > 0:
                await asyncio.sleep(wait)
                now = loop.time()
                while _rate_window and now - _rate_window[0] >= 1.0:
                    _rate_window.popleft()
        _rate_window.append(loop.time())


# --- per-user API client -------------------------------------------------


class NotionClient:
    def __init__(self, *, user_id: UUID, link: links_dao.NotionLink) -> None:
        self._user_id = user_id
        self._link = link

    @classmethod
    async def for_user(cls, user_id: UUID) -> "NotionClient":
        async with acquire() as conn:
            link = await links_dao.get(conn, user_id=user_id)
        if link is None:
            raise NotionNotConnectedError(
                "User has not connected Notion. Run"
                " POST /integrations/notion/install-url to start the"
                " OAuth flow."
            )
        return cls(user_id=user_id, link=link)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._link.access_token}",
            "Notion-Version": _API_VERSION,
            "Content-Type": "application/json",
        }

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        await _rate_limit()
        async with _client_or_new(timeout=30.0) as client:
            resp = await client.post(
                f"{_API_BASE}{path}", headers=self._headers(), json=body,
            )
        if resp.status_code != 200:
            raise NotionApiError(resp.status_code, resp.text)
        return resp.json()

    async def _get(self, path: str) -> dict[str, Any]:
        await _rate_limit()
        async with _client_or_new(timeout=30.0) as client:
            resp = await client.get(
                f"{_API_BASE}{path}", headers=self._headers(),
            )
        if resp.status_code != 200:
            raise NotionApiError(resp.status_code, resp.text)
        return resp.json()

    # --- API ----------------------------------------------------------

    async def search(
        self, query: str, *, page_size: int = 10,
    ) -> list[dict[str, Any]]:
        """Search pages + databases the workspace shares with the bot.
        Returns each result's id, type, title, last_edited_time, url."""
        payload = await self._post(
            "/search",
            {
                "query": query,
                "page_size": page_size,
                "filter": {"value": "page", "property": "object"},
            },
        )
        return [_normalize_search_hit(r) for r in payload.get("results", [])]

    async def read_page(self, page_id: str) -> dict[str, Any]:
        """Fetch a page's properties + its top-level block contents.

        Two API calls: GET /pages/{id} for the properties, then GET
        /blocks/{id}/children for the body. Nested blocks aren't
        recursively fetched here — the agent can call again if it
        needs sub-blocks. Keeps the call small for token economy."""
        page = await self._get(f"/pages/{page_id}")
        blocks = await self._get(f"/blocks/{page_id}/children")
        return {
            "page_id": page_id,
            "url": page.get("url"),
            "title": _extract_page_title(page),
            "properties": page.get("properties", {}),
            "blocks": [
                _normalize_block(b)
                for b in blocks.get("results", [])
            ],
        }

    async def create_page(
        self,
        *,
        parent_page_id: str,
        title: str,
        body_markdown: str | None = None,
    ) -> dict[str, Any]:
        """Create a new page under ``parent_page_id``. ``body_markdown``
        is converted into a paragraph block for simplicity — Notion
        supports much richer block types, but a v1 Tool Creator
        shouldn't need them. The page id is returned so the agent can
        link to it in its final answer."""
        body: dict[str, Any] = {
            "parent": {"page_id": parent_page_id},
            "properties": {
                "title": {
                    "title": [{"text": {"content": title}}],
                },
            },
        }
        if body_markdown:
            body["children"] = [
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {"type": "text", "text": {"content": body_markdown}},
                        ],
                    },
                },
            ]
        result = await self._post("/pages", body)
        return {
            "page_id": result.get("id"),
            "url": result.get("url"),
            "title": title,
        }


# --- helpers -------------------------------------------------------------


def _normalize_search_hit(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record.get("id"),
        "object": record.get("object"),
        "url": record.get("url"),
        "last_edited_time": record.get("last_edited_time"),
        "title": _extract_page_title(record),
    }


def _extract_page_title(record: dict[str, Any]) -> str:
    """Notion's title is buried inside properties[*].title[*].plain_text.
    We try the common shapes + return ``""`` on any miss rather than
    raising — the search result is still useful without the title."""
    props = record.get("properties") or {}
    for prop in props.values():
        if not isinstance(prop, dict) or prop.get("type") != "title":
            continue
        title_arr = prop.get("title") or []
        if title_arr:
            return title_arr[0].get("plain_text", "")
    return ""


def _normalize_block(block: dict[str, Any]) -> dict[str, Any]:
    """Strip Notion's verbose block shape down to {type, text} for the
    agent's prompt budget. Unknown block types fall through with
    empty text."""
    btype = block.get("type")
    text = ""
    body = block.get(btype) if btype else None
    if isinstance(body, dict):
        rich = body.get("rich_text") or body.get("text") or []
        if isinstance(rich, list):
            text = "".join(
                seg.get("plain_text", "") for seg in rich
                if isinstance(seg, dict)
            )
    return {"type": btype, "text": text}
