"""Pluggable email delivery.

`ConsoleEmailBackend` writes the email to the structured log instead of
sending it — useful for local dev and tests. `SESEmailBackend` lands in
step 23 when SES inbound forwarding ships.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from functools import lru_cache

from wolfpaw.config import get_settings
from wolfpaw.tracing import get_logger

log = get_logger()


class EmailBackend(ABC):
    @abstractmethod
    async def send(self, *, to: str, subject: str, body: str) -> None: ...


class ConsoleEmailBackend(EmailBackend):
    """Logs the email; never sends. Default for dev and tests."""

    async def send(self, *, to: str, subject: str, body: str) -> None:
        log.info("email.send.console", to=to, subject=subject, body=body)


@lru_cache
def get_email_backend() -> EmailBackend:
    settings = get_settings()
    if settings.email_backend == "console":
        return ConsoleEmailBackend()
    raise ValueError(f"unknown email_backend: {settings.email_backend!r}")
