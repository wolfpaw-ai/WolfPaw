"""Dropbox HTTP client wrapping the bits of the Dropbox API we use.

Two distinct concerns:

  * **OAuth code exchange + refresh** — :func:`exchange_code` and
    :func:`refresh_access_token` talk to ``api.dropboxapi.com/oauth2/token``.
    Used by the install-callback route and by :class:`DropboxClient`'s
    refresh-before-call logic.

  * **API calls** — :class:`DropboxClient` is constructed per-user and
    per-request. ``list_folder``, ``read_file``, ``write_file`` against
    the App folder. The client checks the token's `expires_at` before
    every call and refreshes (updating the DB) when within
    ``_REFRESH_BUFFER_SECONDS`` of expiry.

The HTTP transport is injectable via :func:`set_http_client_for_test`
so unit tests can drive the OAuth + API flows without real network
calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx

from wolfpaw.config import get_settings
from wolfpaw.memory import dropbox_links as links_dao
from wolfpaw.memory.db import acquire
from wolfpaw.tracing import get_logger

log = get_logger()


_TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
_AUTHORIZE_URL = "https://www.dropbox.com/oauth2/authorize"
_API_BASE = "https://api.dropboxapi.com/2"
_CONTENT_BASE = "https://content.dropboxapi.com/2"

# Refresh tokens this many seconds before they actually expire so the
# next API call doesn't race the clock.
_REFRESH_BUFFER_SECONDS = 60


# Injectable HTTP client for tests.
_http_client: httpx.AsyncClient | None = None


def set_http_client_for_test(client: httpx.AsyncClient | None) -> None:
    """Tests inject a respx-wrapped client; pass None to restore the
    default per-call AsyncClient."""
    global _http_client
    _http_client = client


def _client_or_new(timeout: float) -> httpx.AsyncClient:
    return _http_client or httpx.AsyncClient(timeout=timeout)


class DropboxApiError(Exception):
    """Non-2xx response from the Dropbox API."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"Dropbox API {status_code}: {body}")
        self.status_code = status_code
        self.body = body


class DropboxNotConnectedError(Exception):
    """The user hasn't connected Dropbox yet."""


@dataclass(frozen=True)
class TokenExchangeResult:
    access_token: str
    refresh_token: str
    expires_at: datetime
    account_id: str | None
    scope: str | None


# --- OAuth helpers -------------------------------------------------------


def build_authorize_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    """Build the Dropbox authorize URL the user is redirected to.

    ``token_access_type=offline`` requests a refresh token (default in
    short-lived-token mode is no refresh token). ``response_type=code``
    is the standard authorization-code flow. ``state`` is the
    integration_state_token we minted and that we'll verify on the
    callback. App-folder scope is selected on the Dropbox app side, so
    we don't need to pass scope here."""
    from urllib.parse import urlencode

    qs = urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "token_access_type": "offline",
        "state": state,
    })
    return f"{_AUTHORIZE_URL}?{qs}"


async def exchange_code(
    *, code: str, redirect_uri: str,
    client_id: str, client_secret: str,
) -> TokenExchangeResult:
    """Trade an OAuth authorization code for the initial token bundle.
    Called by the callback route."""
    settings = get_settings()
    async with _client_or_new(timeout=15.0) as client:
        resp = await client.post(
            _TOKEN_URL,
            data={
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
    if resp.status_code != 200:
        raise DropboxApiError(resp.status_code, resp.text)
    payload = resp.json()
    return _parse_token_response(payload)


async def refresh_access_token(
    *, refresh_token: str, client_id: str, client_secret: str,
) -> TokenExchangeResult:
    """Trade a refresh token for a fresh access token. Dropbox usually
    returns the same refresh_token; rotation is uncommon but possible."""
    async with _client_or_new(timeout=15.0) as client:
        resp = await client.post(
            _TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
    if resp.status_code != 200:
        raise DropboxApiError(resp.status_code, resp.text)
    payload = resp.json()
    # Carry the input refresh_token forward if Dropbox didn't return one
    # (it rarely does on refresh).
    refreshed = _parse_token_response(payload)
    if not refreshed.refresh_token:
        refreshed = TokenExchangeResult(
            access_token=refreshed.access_token,
            refresh_token=refresh_token,
            expires_at=refreshed.expires_at,
            account_id=refreshed.account_id,
            scope=refreshed.scope,
        )
    return refreshed


def _parse_token_response(payload: dict[str, Any]) -> TokenExchangeResult:
    expires_in = int(payload.get("expires_in", 0))
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    return TokenExchangeResult(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token", ""),
        expires_at=expires_at,
        account_id=payload.get("account_id"),
        scope=payload.get("scope"),
    )


# --- per-user API client -------------------------------------------------


class DropboxClient:
    """Per-user, per-request Dropbox API client. Construct via
    :meth:`for_user` so token refresh happens transparently."""

    def __init__(self, *, user_id: UUID, link: links_dao.DropboxLink) -> None:
        self._user_id = user_id
        self._link = link

    @classmethod
    async def for_user(cls, user_id: UUID) -> "DropboxClient":
        """Build a client against the user's current Dropbox tokens.
        Raises :class:`DropboxNotConnectedError` if the user hasn't
        connected Dropbox."""
        async with acquire() as conn:
            link = await links_dao.get(conn, user_id=user_id)
        if link is None:
            raise DropboxNotConnectedError(
                "User has not connected Dropbox. Run"
                " POST /integrations/dropbox/install-url to start the"
                " OAuth flow."
            )
        return cls(user_id=user_id, link=link)

    async def _ensure_fresh_token(self) -> str:
        """Refresh the access token if it's within the buffer of
        expiry. Returns the access token to use."""
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(seconds=_REFRESH_BUFFER_SECONDS)
        if self._link.expires_at > cutoff:
            return self._link.access_token
        settings = get_settings()
        result = await refresh_access_token(
            refresh_token=self._link.refresh_token,
            client_id=settings.dropbox_client_id,
            client_secret=settings.dropbox_client_secret,
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
        # Update in-memory link so subsequent calls in the same request
        # don't re-refresh.
        self._link = links_dao.DropboxLink(
            user_id=self._user_id,
            access_token=result.access_token,
            refresh_token=result.refresh_token or self._link.refresh_token,
            expires_at=result.expires_at,
            account_id=self._link.account_id,
            scope=self._link.scope,
            created_at=self._link.created_at,
            updated_at=now,
        )
        return result.access_token

    async def _api_post(
        self, *, path: str, body: dict[str, Any],
    ) -> dict[str, Any]:
        token = await self._ensure_fresh_token()
        async with _client_or_new(timeout=30.0) as client:
            resp = await client.post(
                f"{_API_BASE}{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        if resp.status_code != 200:
            raise DropboxApiError(resp.status_code, resp.text)
        return resp.json()

    async def _content_post(
        self, *, path: str, headers: dict[str, str], data: bytes,
    ) -> bytes:
        token = await self._ensure_fresh_token()
        merged = {"Authorization": f"Bearer {token}", **headers}
        async with _client_or_new(timeout=60.0) as client:
            resp = await client.post(
                f"{_CONTENT_BASE}{path}",
                headers=merged,
                content=data,
            )
        if resp.status_code != 200:
            raise DropboxApiError(resp.status_code, resp.text)
        return resp.content

    # --- API calls ------------------------------------------------------

    async def list_folder(self, path: str = "") -> list[dict[str, Any]]:
        """List entries at ``path`` relative to the App folder. Empty
        string means the App folder root. Returns a list of entries
        with name/path/size/is_folder."""
        payload = await self._api_post(
            path="/files/list_folder", body={"path": path or ""},
        )
        return [
            {
                "name": entry.get("name"),
                "path": entry.get("path_display"),
                "is_folder": entry.get(".tag") == "folder",
                "size_bytes": entry.get("size"),
                "client_modified": entry.get("client_modified"),
                "server_modified": entry.get("server_modified"),
            }
            for entry in payload.get("entries", [])
        ]

    async def read_file(self, path: str) -> bytes:
        """Download the file at ``path`` (App-folder-relative). Returns
        the raw bytes; callers decode as needed."""
        return await self._content_post(
            path="/files/download",
            headers={
                "Dropbox-API-Arg": json.dumps({"path": path}),
            },
            data=b"",
        )

    async def write_file(
        self, path: str, content: bytes, *, overwrite: bool = False,
    ) -> dict[str, Any]:
        """Upload ``content`` to ``path``. ``overwrite=True`` replaces
        an existing file; default is ``add`` mode which refuses to
        overwrite (Dropbox returns a conflict response)."""
        mode = "overwrite" if overwrite else "add"
        result_bytes = await self._content_post(
            path="/files/upload",
            headers={
                "Content-Type": "application/octet-stream",
                "Dropbox-API-Arg": json.dumps({
                    "path": path,
                    "mode": mode,
                    "autorename": False,
                    "mute": True,
                }),
            },
            data=content,
        )
        # Response is JSON, even though it came back on the content
        # endpoint.
        return json.loads(result_bytes.decode("utf-8"))
