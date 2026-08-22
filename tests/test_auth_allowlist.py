"""Sign-in allowlist + SES email backend.

No DB needed — the allowlist is pure config, and the SES backend takes an
injected client.
"""

from __future__ import annotations

import pytest

from wolfpaw.auth.allowlist import allowed_emails, email_allowed
from wolfpaw.auth.email_backend import (
    ConsoleEmailBackend,
    SESEmailBackend,
    get_email_backend,
)
from wolfpaw.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()  # type: ignore[attr-defined]
    get_email_backend.cache_clear()  # type: ignore[attr-defined]
    yield
    get_settings.cache_clear()  # type: ignore[attr-defined]
    get_email_backend.cache_clear()  # type: ignore[attr-defined]


# --- allowlist ---------------------------------------------------------------


def test_empty_allowlist_permits_everyone(monkeypatch):
    monkeypatch.setenv("WOLFPAW_ALLOWED_EMAILS", "")
    assert allowed_emails() == frozenset()
    assert email_allowed("anyone@example.com") is True


def test_allowlist_permits_listed_address(monkeypatch):
    monkeypatch.setenv(
        "WOLFPAW_ALLOWED_EMAILS", "dmitri@example.com,rory@example.com"
    )
    assert email_allowed("dmitri@example.com") is True
    assert email_allowed("rory@example.com") is True


def test_allowlist_rejects_unlisted_address(monkeypatch):
    monkeypatch.setenv("WOLFPAW_ALLOWED_EMAILS", "dmitri@example.com")
    assert email_allowed("stranger@example.com") is False


def test_allowlist_is_case_and_whitespace_insensitive(monkeypatch):
    monkeypatch.setenv(
        "WOLFPAW_ALLOWED_EMAILS", "  Dmitri@Example.com , rory@example.com  "
    )
    assert email_allowed("dmitri@example.com") is True
    assert email_allowed("  RORY@EXAMPLE.COM ") is True


def test_allowlist_ignores_empty_entries(monkeypatch):
    """A trailing comma must not admit the empty string."""
    monkeypatch.setenv("WOLFPAW_ALLOWED_EMAILS", "dmitri@example.com,,")
    assert allowed_emails() == frozenset({"dmitri@example.com"})
    assert email_allowed("") is False


# --- SES backend -------------------------------------------------------------


class FakeSESClient:
    def __init__(self, exc: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._exc = exc

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc:
            raise self._exc
        return {"MessageId": "msg-123"}


async def test_ses_sends_expected_payload():
    fake = FakeSESClient()
    backend = SESEmailBackend(
        region="us-east-1", from_email="no-reply@wolfpaw.ai", client=fake
    )
    await backend.send(to="rory@example.com", subject="Hi", body="Link here")

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["Source"] == "no-reply@wolfpaw.ai"
    assert call["Destination"]["ToAddresses"] == ["rory@example.com"]
    assert call["Message"]["Subject"]["Data"] == "Hi"
    assert call["Message"]["Body"]["Text"]["Data"] == "Link here"
    assert "ConfigurationSetName" not in call


async def test_ses_includes_configuration_set_when_set():
    fake = FakeSESClient()
    backend = SESEmailBackend(
        region="us-east-1",
        from_email="no-reply@wolfpaw.ai",
        configuration_set="wolfpaw-tracking",
        client=fake,
    )
    await backend.send(to="rory@example.com", subject="Hi", body="Link")
    assert fake.calls[0]["ConfigurationSetName"] == "wolfpaw-tracking"


async def test_ses_swallows_send_failure():
    """A send failure must not surface as a 500 — that would let a caller
    tell verified recipients from unverified ones in the SES sandbox."""
    fake = FakeSESClient(exc=RuntimeError("MessageRejected"))
    backend = SESEmailBackend(
        region="us-east-1", from_email="no-reply@wolfpaw.ai", client=fake
    )
    await backend.send(to="rory@example.com", subject="Hi", body="Link")
    assert len(fake.calls) == 1


def test_ses_requires_from_email():
    with pytest.raises(ValueError, match="WOLFPAW_SES_FROM_EMAIL"):
        SESEmailBackend(region="us-east-1", from_email="", client=FakeSESClient())


# --- route behavior ----------------------------------------------------------
#
# A rejected address returns before `acquire()` is ever called, so these run
# without a database. The allowed-address path is covered in test_auth_flow.py,
# which is DB-gated.


def _client_with_capture():
    from fastapi.testclient import TestClient

    from wolfpaw.api import create_app
    from wolfpaw.auth.email_backend import EmailBackend

    class CapturingEmailBackend(EmailBackend):
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send(self, *, to: str, subject: str, body: str) -> None:
            self.sent.append({"to": to, "subject": subject, "body": body})

    app = create_app()
    captured = CapturingEmailBackend()
    app.dependency_overrides[get_email_backend] = lambda: captured
    return TestClient(app), captured


def test_rejected_address_gets_202_and_no_email(monkeypatch):
    """Indistinguishable from the happy path, and nothing is sent."""
    monkeypatch.setenv("WOLFPAW_ALLOWED_EMAILS", "dmitri@example.com")
    client, captured = _client_with_capture()

    r = client.post("/auth/magic-link", json={"email": "stranger@example.com"})
    assert r.status_code == 202
    assert r.json() == {"status": "ok"}
    assert captured.sent == []


# --- backend selection -------------------------------------------------------


def test_console_backend_is_the_default(monkeypatch):
    monkeypatch.setenv("WOLFPAW_EMAIL_BACKEND", "console")
    assert isinstance(get_email_backend(), ConsoleEmailBackend)


def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("WOLFPAW_EMAIL_BACKEND", "carrier-pigeon")
    with pytest.raises(ValueError, match="carrier-pigeon"):
        get_email_backend()
