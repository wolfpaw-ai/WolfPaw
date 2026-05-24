"""Thin async wrapper over Slack's Web API.

v1 surface area:
    - `oauth_v2_access(code)`  → exchanges an OAuth code at install time
    - `chat_post_message(...)` → sends a reply to a DM or channel

Two clients ship: the real `HttpSlackClient` (httpx) and `FakeSlackClient`
for tests. The factory honors a process-wide override so tests can
inject the fake during `create_app()`.

Slack's API returns `{"ok": true, ...}` on success and `{"ok": false,
"error": "..."}` on failure. We surface failures as `SlackApiError`
rather than passing the raw dict back — keeps callers from having to
check `["ok"]` everywhere.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from wolfpaw.tracing import get_logger

log = get_logger()


class SlackApiError(Exception):
    """Slack returned `{"ok": false}`. The wrapped error string is what
    Slack's docs call `error` (e.g. `invalid_auth`, `channel_not_found`)."""

    def __init__(self, endpoint: str, error: str, *, payload: dict | None = None):
        super().__init__(f"slack {endpoint}: {error}")
        self.endpoint = endpoint
        self.error = error
        self.payload = payload or {}


class SlackClient(ABC):
    @abstractmethod
    async def oauth_v2_access(
        self, *, code: str, client_id: str, client_secret: str,
        redirect_uri: str,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def chat_post_message(
        self, *, bot_token: str, channel: str, text: str,
    ) -> dict[str, Any]: ...


class HttpSlackClient(SlackClient):
    """Production client — talks to https://slack.com/api."""

    BASE_URL = "https://slack.com/api"

    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        self._timeout = timeout_seconds

    async def oauth_v2_access(
        self, *, code: str, client_id: str, client_secret: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        # oauth.v2.access accepts form-encoded; Slack auths via the form
        # client_id + client_secret (not Basic auth).
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self.BASE_URL}/oauth.v2.access",
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                },
            )
            resp.raise_for_status()
            payload = resp.json()
        if not payload.get("ok"):
            raise SlackApiError(
                "oauth.v2.access", payload.get("error", "unknown_error"),
                payload=payload,
            )
        return payload

    async def chat_post_message(
        self, *, bot_token: str, channel: str, text: str,
    ) -> dict[str, Any]:
        # Slack's outbound text limit is ~40KB but anything past ~4KB
        # gets visually awkward — truncate at 4096 like Telegram.
        if len(text) > 4096:
            text = text[:4090] + "…"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self.BASE_URL}/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {bot_token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json={"channel": channel, "text": text},
            )
            resp.raise_for_status()
            payload = resp.json()
        if not payload.get("ok"):
            raise SlackApiError(
                "chat.postMessage", payload.get("error", "unknown_error"),
                payload=payload,
            )
        return payload


@dataclass
class FakeSlackClient(SlackClient):
    """Captures outbound calls for tests + returns canned OAuth responses.

    Usage:
        fake = FakeSlackClient(oauth_response={...})
        set_slack_client(fake)
        ... run code ...
        assert fake.sent == [...]
    """

    oauth_response: dict[str, Any] | None = None
    sent: list[dict[str, Any]] = field(default_factory=list)
    oauth_calls: list[dict[str, Any]] = field(default_factory=list)

    async def oauth_v2_access(
        self, *, code: str, client_id: str, client_secret: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        self.oauth_calls.append({
            "code": code, "client_id": client_id,
            "redirect_uri": redirect_uri,
        })
        if self.oauth_response is None:
            raise SlackApiError(
                "oauth.v2.access", "no canned response set on fake",
            )
        return self.oauth_response

    async def chat_post_message(
        self, *, bot_token: str, channel: str, text: str,
    ) -> dict[str, Any]:
        record = {"bot_token": bot_token, "channel": channel, "text": text}
        self.sent.append(record)
        log.info("slack.fake.send", channel=channel, text_len=len(text))
        return {"ok": True, "ts": str(len(self.sent))}


_client: SlackClient | None = None


def get_slack_client() -> SlackClient:
    """Singleton accessor. Builds an HttpSlackClient lazily; tests
    override via `set_slack_client(fake)`."""
    global _client
    if _client is None:
        _client = HttpSlackClient()
    return _client


def set_slack_client(client: SlackClient) -> None:
    global _client
    _client = client


def reset_slack_client() -> None:
    global _client
    _client = None
