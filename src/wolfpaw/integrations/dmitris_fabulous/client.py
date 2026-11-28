"""HTTP client for the Document Organizer public API (``/v1``).

Wolfpaw is customer #1 of that API and uses nothing else: no internal
routes, no shared database. The client is constructed per user from the
key stored in ``integration_links`` and maps the API's error envelope
(``{"error": {"code", "message", "request_id"}}``) onto one exception
type the tools turn into user-readable ``ToolError`` text.

The HTTP transport is injectable via :func:`set_http_client_for_test`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx

from wolfpaw.config import get_settings
from wolfpaw.memory import integration_links as links_dao
from wolfpaw.memory.db import acquire
from wolfpaw.tracing import get_logger

log = get_logger()

PROVIDER = "dmitris_fabulous"

_SHORT_TIMEOUT = 20.0
_MAX_RETRY_AFTER_SECONDS = 10.0

_http_client: httpx.AsyncClient | None = None


def set_http_client_for_test(client: httpx.AsyncClient | None) -> None:
    global _http_client
    _http_client = client


def _client_or_new(timeout: float) -> httpx.AsyncClient:
    return _http_client or httpx.AsyncClient(timeout=timeout)


class DocumentOrganizerError(Exception):
    """The API returned an error. ``code`` is the stable error code from
    the response envelope (``invalid_token``, ``quota_exceeded``,
    ``rate_limited``, ``not_found``, ``agent_failed`` …) or ``http_<n>``
    / ``network`` for failures without an envelope."""

    def __init__(self, status: int, code: str, message: str, request_id: str | None = None):
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id


class DocumentOrganizerNotConnectedError(Exception):
    """The user hasn't connected the Document Organizer."""


@dataclass(frozen=True)
class AgentOutcome:
    """Search/report responses carry the request id for support."""

    body: dict[str, Any]
    request_id: str | None


def api_base() -> str:
    base = get_settings().df_base_url.strip().rstrip("/")
    return f"{base}/v1" if base else ""


def _raise_for(resp: httpx.Response) -> None:
    request_id = resp.headers.get("request-id")
    try:
        err = resp.json().get("error") or {}
    except Exception:  # noqa: BLE001
        err = {}
    raise DocumentOrganizerError(
        resp.status_code,
        err.get("code") or f"http_{resp.status_code}",
        err.get("message") or (resp.text or "request failed")[:200],
        err.get("request_id") or request_id,
    )


class DocumentOrganizerClient:
    def __init__(self, *, api_key: str, base: str | None = None) -> None:
        self._api_key = api_key
        self._base = base if base is not None else api_base()

    @classmethod
    async def for_user(cls, user_id: UUID) -> "DocumentOrganizerClient":
        async with acquire() as conn:
            link = await links_dao.get(conn, user_id=user_id, provider=PROVIDER)
        if link is None or not link.credentials.get("api_key"):
            raise DocumentOrganizerNotConnectedError(
                "The Document Organizer isn't connected. Paste an API key"
                " under Profile → Document Organizer."
            )
        return cls(api_key=link.credentials["api_key"])

    # -- transport --------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "User-Agent": "wolfpaw",
        }

    async def _request(
        self, method: str, path: str, *, timeout: float = _SHORT_TIMEOUT,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        raw: bool = False,
    ) -> Any:
        """One call, with a single retry on 429 (honoring Retry-After) or
        a transient 5xx/network error. Agent-backed calls (long timeout)
        retry only on 429: re-running a minute-long search would double the
        cost for little gain."""
        if not self._base:
            raise DocumentOrganizerError(
                0, "not_configured", "WOLFPAW_DF_BASE_URL is not set on this deployment",
            )
        long_call = timeout > _SHORT_TIMEOUT
        url = f"{self._base}{path}"
        last: DocumentOrganizerError | None = None
        for attempt in (1, 2):
            try:
                async with _client_or_new(timeout=timeout) as client:
                    resp = await client.request(
                        method, url, headers=self._headers(),
                        json=json_body, params=params,
                        follow_redirects=False,
                    )
            except httpx.HTTPError as e:
                last = DocumentOrganizerError(0, "network", f"could not reach the Document Organizer ({type(e).__name__})")
                if attempt == 1 and not long_call:
                    await asyncio.sleep(0.5)
                    continue
                raise last from e

            if resp.status_code < 400:
                return resp if raw else resp.json()

            try:
                _raise_for(resp)
            except DocumentOrganizerError as err:
                last = err
                retryable = err.code == "rate_limited" or (
                    resp.status_code >= 500 and not long_call
                )
                if attempt == 1 and retryable:
                    delay = 0.5
                    if err.code == "rate_limited":
                        try:
                            delay = min(float(resp.headers.get("retry-after", "1")), _MAX_RETRY_AFTER_SECONDS)
                        except ValueError:
                            delay = 1.0
                    await asyncio.sleep(delay)
                    continue
                raise
        assert last is not None
        raise last

    # -- API --------------------------------------------------------------

    async def me(self) -> dict[str, Any]:
        return await self._request("GET", "/me")

    async def lists(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/lists")).get("lists", [])

    async def search(
        self, query: str, *, list_id: str | None = None, brief: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"query": query}
        if list_id:
            body["list_id"] = list_id
        if brief:
            body["answer_style"] = "brief"
        return await self._request(
            "POST", "/search", json_body=body,
            timeout=get_settings().df_agent_timeout_seconds,
        )

    async def get_document(self, doc_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/documents/{doc_id}")

    async def get_document_text(self, doc_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/documents/{doc_id}/text")

    async def document_file(self, doc_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/documents/{doc_id}/file")

    async def report(
        self, query: str, *, output_types: list[str] | None = None,
        list_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"query": query}
        if output_types:
            body["output_types"] = output_types
        if list_id:
            body["list_id"] = list_id
        return await self._request(
            "POST", "/reports", json_body=body,
            timeout=get_settings().df_agent_timeout_seconds,
        )

    async def export_report(self, report: dict[str, Any], *, fmt: str, query: str | None = None) -> bytes:
        body: dict[str, Any] = {"format": fmt, "report": report}
        if query:
            body["query"] = query
        resp = await self._request("POST", "/reports/export", json_body=body, raw=True)
        return resp.content
