"""Microsoft Graph HTTP client (step 32).

OAuth + per-user API client. Same shape as the Dropbox client
(refresh-tokens, expiry-buffer auto-refresh) but talks to
``login.microsoftonline.com`` for OAuth and ``graph.microsoft.com``
for calendar operations.

Scopes requested:
  * ``Calendars.ReadWrite`` — read + create events
  * ``offline_access`` — get a refresh_token
  * ``User.Read`` — minimal profile, used to populate ``account_id``
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx

from wolfpaw.config import get_settings
from wolfpaw.memory import microsoft_links as links_dao
from wolfpaw.memory.db import acquire
from wolfpaw.tracing import get_logger

log = get_logger()


_AUTHORIZE_URL_TEMPLATE = (
    "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
)
_TOKEN_URL_TEMPLATE = (
    "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
)
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_SCOPES = "Calendars.ReadWrite offline_access User.Read"

_REFRESH_BUFFER_SECONDS = 60


_http_client: httpx.AsyncClient | None = None


def set_http_client_for_test(client: httpx.AsyncClient | None) -> None:
    global _http_client
    _http_client = client


def _client_or_new(timeout: float) -> httpx.AsyncClient:
    return _http_client or httpx.AsyncClient(timeout=timeout)


class MicrosoftApiError(Exception):
    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"Microsoft API {status_code}: {body}")
        self.status_code = status_code
        self.body = body


class MicrosoftNotConnectedError(Exception):
    """The user hasn't connected Microsoft yet."""


@dataclass(frozen=True)
class TokenExchangeResult:
    access_token: str
    refresh_token: str
    expires_at: datetime
    scope: str | None


# --- OAuth helpers -------------------------------------------------------


def build_authorize_url(
    *, tenant: str, client_id: str, redirect_uri: str, state: str,
) -> str:
    from urllib.parse import urlencode

    qs = urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": _SCOPES,
        "state": state,
    })
    return _AUTHORIZE_URL_TEMPLATE.format(tenant=tenant) + "?" + qs


async def exchange_code(
    *, code: str, redirect_uri: str, tenant: str,
    client_id: str, client_secret: str,
) -> TokenExchangeResult:
    async with _client_or_new(timeout=15.0) as client:
        resp = await client.post(
            _TOKEN_URL_TEMPLATE.format(tenant=tenant),
            data={
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": _SCOPES,
            },
        )
    if resp.status_code != 200:
        raise MicrosoftApiError(resp.status_code, resp.text)
    payload = resp.json()
    return _parse_token_response(payload)


async def refresh_access_token(
    *, refresh_token: str, tenant: str,
    client_id: str, client_secret: str,
) -> TokenExchangeResult:
    async with _client_or_new(timeout=15.0) as client:
        resp = await client.post(
            _TOKEN_URL_TEMPLATE.format(tenant=tenant),
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": _SCOPES,
            },
        )
    if resp.status_code != 200:
        raise MicrosoftApiError(resp.status_code, resp.text)
    refreshed = _parse_token_response(resp.json())
    # Microsoft sometimes returns a fresh refresh_token, sometimes not.
    # If it's blank, carry the old one forward.
    if not refreshed.refresh_token:
        refreshed = TokenExchangeResult(
            access_token=refreshed.access_token,
            refresh_token=refresh_token,
            expires_at=refreshed.expires_at,
            scope=refreshed.scope,
        )
    return refreshed


def _parse_token_response(payload: dict[str, Any]) -> TokenExchangeResult:
    expires_in = int(payload.get("expires_in", 0))
    return TokenExchangeResult(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token", ""),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        scope=payload.get("scope"),
    )


# --- per-user API client -------------------------------------------------


class MicrosoftClient:
    def __init__(self, *, user_id: UUID, link: links_dao.MicrosoftLink) -> None:
        self._user_id = user_id
        self._link = link

    @classmethod
    async def for_user(cls, user_id: UUID) -> "MicrosoftClient":
        async with acquire() as conn:
            link = await links_dao.get(conn, user_id=user_id)
        if link is None:
            raise MicrosoftNotConnectedError(
                "User has not connected Microsoft. Run"
                " POST /integrations/microsoft/install-url to start."
            )
        return cls(user_id=user_id, link=link)

    async def _ensure_fresh_token(self) -> str:
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(seconds=_REFRESH_BUFFER_SECONDS)
        if self._link.expires_at > cutoff:
            return self._link.access_token
        settings = get_settings()
        result = await refresh_access_token(
            refresh_token=self._link.refresh_token,
            tenant=settings.microsoft_tenant,
            client_id=settings.microsoft_client_id,
            client_secret=settings.microsoft_client_secret,
        )
        async with acquire() as conn:
            await links_dao.update_tokens(
                conn,
                user_id=self._user_id,
                access_token=result.access_token,
                expires_at=result.expires_at,
                refresh_token=(
                    result.refresh_token
                    if result.refresh_token != self._link.refresh_token
                    else None
                ),
            )
        self._link = links_dao.MicrosoftLink(
            user_id=self._user_id,
            access_token=result.access_token,
            refresh_token=result.refresh_token or self._link.refresh_token,
            expires_at=result.expires_at,
            tenant_id=self._link.tenant_id,
            account_id=self._link.account_id,
            scope=self._link.scope,
            created_at=self._link.created_at,
            updated_at=now,
        )
        return result.access_token

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def _get(
        self, path: str, params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        token = await self._ensure_fresh_token()
        async with _client_or_new(timeout=30.0) as client:
            resp = await client.get(
                f"{_GRAPH_BASE}{path}",
                headers=self._headers(token),
                params=params,
            )
        if resp.status_code != 200:
            raise MicrosoftApiError(resp.status_code, resp.text)
        return resp.json()

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        token = await self._ensure_fresh_token()
        async with _client_or_new(timeout=30.0) as client:
            resp = await client.post(
                f"{_GRAPH_BASE}{path}",
                headers=self._headers(token),
                json=body,
            )
        # Graph returns 200 on read, 201 on event create.
        if resp.status_code not in (200, 201):
            raise MicrosoftApiError(resp.status_code, resp.text)
        return resp.json()

    # --- calendar -----------------------------------------------------

    async def list_events(
        self,
        *,
        start: str,
        end: str,
        max_results: int = 20,
    ) -> list[dict[str, Any]]:
        """List events between two ISO-8601 timestamps. Uses the
        ``/calendarView`` endpoint which expands recurring events into
        instances within the window."""
        params = {
            "startDateTime": start,
            "endDateTime": end,
            "$top": str(max_results),
            "$orderby": "start/dateTime",
        }
        payload = await self._get("/me/calendarView", params=params)
        return [_normalize_event(e) for e in payload.get("value", [])]

    async def create_event(
        self,
        *,
        subject: str,
        start: str,
        end: str,
        body_html: str | None = None,
        attendees: list[str] | None = None,
        location: str | None = None,
        time_zone: str = "UTC",
    ) -> dict[str, Any]:
        """Create a calendar event. Times are ISO-8601 strings;
        ``time_zone`` is an IANA zone or Windows zone name."""
        body: dict[str, Any] = {
            "subject": subject,
            "start": {"dateTime": start, "timeZone": time_zone},
            "end": {"dateTime": end, "timeZone": time_zone},
        }
        if body_html:
            body["body"] = {"contentType": "HTML", "content": body_html}
        if attendees:
            body["attendees"] = [
                {
                    "emailAddress": {"address": addr, "name": addr},
                    "type": "required",
                }
                for addr in attendees
            ]
        if location:
            body["location"] = {"displayName": location}
        result = await self._post("/me/events", body)
        return _normalize_event(result)


# --- helpers -------------------------------------------------------------


def _normalize_event(record: dict[str, Any]) -> dict[str, Any]:
    """Strip the verbose Graph event shape down to the fields the
    agent will actually use. Missing fields drop to None."""
    start = record.get("start") or {}
    end = record.get("end") or {}
    return {
        "id": record.get("id"),
        "subject": record.get("subject"),
        "start": start.get("dateTime"),
        "end": end.get("dateTime"),
        "time_zone": start.get("timeZone"),
        "location": (record.get("location") or {}).get("displayName"),
        "attendees": [
            (a.get("emailAddress") or {}).get("address")
            for a in record.get("attendees") or []
        ],
        "web_link": record.get("webLink"),
        "is_all_day": record.get("isAllDay", False),
    }
