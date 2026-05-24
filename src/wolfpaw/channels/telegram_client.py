"""Thin async wrapper over Telegram's Bot API.

We only need `sendMessage` for v1 — webhook acknowledgements use plain
HTTP 200, agent responses are pushed back via this client. File uploads,
inline keyboards, voice messages, edits, etc. are future work.

The real client is HTTP-backed via httpx; a `FakeTelegramClient` shipped
alongside lets tests assert on what was sent without making network
calls. The `get_telegram_client()` factory honors a process-wide
override so tests can inject the fake during `create_app()`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from wolfpaw.config import get_settings
from wolfpaw.tracing import get_logger

log = get_logger()


class TelegramClient(ABC):
    @abstractmethod
    async def send_message(
        self, *, chat_id: str | int, text: str,
    ) -> dict[str, Any]: ...


class HttpTelegramClient(TelegramClient):
    """Production client — calls https://api.telegram.org/bot<TOKEN>/sendMessage."""

    def __init__(self, *, token: str, timeout_seconds: float = 30.0) -> None:
        if not token:
            raise ValueError(
                "HttpTelegramClient requires WOLFPAW_TELEGRAM_BOT_TOKEN"
            )
        self._token = token
        self._timeout = timeout_seconds

    @property
    def _base(self) -> str:
        return f"https://api.telegram.org/bot{self._token}"

    async def send_message(
        self, *, chat_id: str | int, text: str,
    ) -> dict[str, Any]:
        # Telegram caps text at 4096 chars per message; truncate rather
        # than splitting — v1's a single message per agent turn.
        if len(text) > 4096:
            text = text[:4090] + "…"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._base}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
            resp.raise_for_status()
            return resp.json()


@dataclass
class FakeTelegramClient(TelegramClient):
    """Captures sent messages for tests. Use:

        fake = FakeTelegramClient()
        set_telegram_client(fake)
        ... run code ...
        assert fake.sent == [...]
    """

    sent: list[dict[str, Any]] = field(default_factory=list)

    async def send_message(
        self, *, chat_id: str | int, text: str,
    ) -> dict[str, Any]:
        record = {"chat_id": chat_id, "text": text}
        self.sent.append(record)
        log.info("telegram.fake.send", **record)
        return {"ok": True, "result": {"message_id": len(self.sent)}}


_client: TelegramClient | None = None


def get_telegram_client() -> TelegramClient:
    """Singleton accessor. Lazy-built from config; tests can override
    via `set_telegram_client(fake)`."""
    global _client
    if _client is not None:
        return _client
    settings = get_settings()
    _client = HttpTelegramClient(token=settings.telegram_bot_token)
    return _client


def set_telegram_client(client: TelegramClient) -> None:
    """Install a specific client (test fake, or a fully-built production
    client). Returns silently — subsequent `get_telegram_client()` calls
    return what was set."""
    global _client
    _client = client


def reset_telegram_client() -> None:
    global _client
    _client = None
