"""Pluggable email delivery.

`ConsoleEmailBackend` writes the email to the structured log instead of
sending it — the default for dev, tests, and any self-host that hasn't
wired a provider. `SESEmailBackend` sends through Amazon SES.

The boto3 dep is gated behind the `[ses]` extra; importing this module
won't pull boto3 in unless an SESEmailBackend is actually constructed.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Any

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


class SESEmailBackend(EmailBackend):
    """Sends via Amazon SES.

    Credentials resolve through the standard boto3 chain — on EC2 that's
    the instance role, so no keys need to live in `.env`. Note that a
    brand-new SES account is in the *sandbox*, where every recipient must
    be individually verified in the console; that's usually fine for a
    small deployment and can be lifted by requesting production access.
    """

    def __init__(
        self,
        *,
        region: str,
        from_email: str,
        configuration_set: str = "",
        client: Any | None = None,
    ) -> None:
        if not from_email:
            raise ValueError(
                "SESEmailBackend requires WOLFPAW_SES_FROM_EMAIL to be set"
                " to an SES-verified sender address."
            )
        self._region = region
        self._from = from_email
        self._configuration_set = configuration_set
        self._client = client or self._build_client(region)

    @staticmethod
    def _build_client(region: str) -> Any:
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as e:
            raise ImportError(
                "boto3 is required for SESEmailBackend. Install with"
                " `pip install wolfpaw[ses]`."
            ) from e
        return boto3.client("ses", region_name=region)

    async def send(self, *, to: str, subject: str, body: str) -> None:
        kwargs: dict[str, Any] = {
            "Source": self._from,
            "Destination": {"ToAddresses": [to]},
            "Message": {
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
            },
        }
        if self._configuration_set:
            kwargs["ConfigurationSetName"] = self._configuration_set

        def _send() -> str:
            resp = self._client.send_email(**kwargs)
            return resp.get("MessageId", "")

        try:
            message_id = await asyncio.to_thread(_send)
        except Exception:
            # Swallow rather than propagate. In the SES sandbox an
            # unverified recipient raises, so a 500 here would let a caller
            # tell verified addresses from unverified ones — exactly the
            # enumeration the allowlist's uniform 202 is there to prevent.
            # The operator gets the failure from this log line.
            log.exception("email.send.ses.failed", to=to, subject=subject)
            return
        log.info("email.send.ses", to=to, subject=subject, message_id=message_id)


@lru_cache
def get_email_backend() -> EmailBackend:
    settings = get_settings()
    if settings.email_backend == "console":
        return ConsoleEmailBackend()
    if settings.email_backend == "ses":
        return SESEmailBackend(
            region=settings.ses_region,
            from_email=settings.ses_from_email,
            configuration_set=settings.ses_configuration_set,
        )
    raise ValueError(f"unknown email_backend: {settings.email_backend!r}")
