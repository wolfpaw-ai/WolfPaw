"""End-to-end magic-link flow against the test DB.

Skipped unless `WOLFPAW_TEST_DATABASE_URL` is set (same convention as
test_migrations.py).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List

import asyncpg
import pytest
from fastapi.testclient import TestClient

from wolfpaw.api import create_app
from wolfpaw.auth.email_backend import EmailBackend, get_email_backend
from wolfpaw.config import get_settings
from wolfpaw.memory.db import apply_sql_file, close_pool, migrations_dir

pytestmark = pytest.mark.skipif(
    not os.getenv("WOLFPAW_TEST_DATABASE_URL"),
    reason="WOLFPAW_TEST_DATABASE_URL not set",
)


class CapturingEmailBackend(EmailBackend):
    def __init__(self) -> None:
        self.sent: List[dict] = []

    async def send(self, *, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body})


@pytest.fixture(autouse=True)
async def _fresh_db(monkeypatch):
    """Wipe + migrate the test DB and point the pool at it."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    monkeypatch.setenv("WOLFPAW_DATABASE_URL", dsn)
    get_settings.cache_clear()  # type: ignore[attr-defined]

    conn = await asyncpg.connect(dsn=dsn)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await apply_sql_file(conn, migrations_dir() / "001_init.sql")
        await apply_sql_file(conn, migrations_dir() / "002_auth.sql")
    finally:
        await conn.close()

    await close_pool()
    yield
    await close_pool()
    get_settings.cache_clear()  # type: ignore[attr-defined]


def _client_with_capture() -> tuple[TestClient, CapturingEmailBackend]:
    app = create_app()
    captured = CapturingEmailBackend()
    app.dependency_overrides[get_email_backend] = lambda: captured
    return TestClient(app), captured


def _extract_token(body: str) -> str:
    """Pull the token out of the email body.

    Matches on the query param rather than a word index — the previous
    `body.split()[3]` indexing broke the moment the copy changed.
    """
    m = re.search(r"/auth/verify\?token=(\S+)", body)
    assert m is not None, f"no verify URL in body: {body!r}"
    return m.group(1)


async def test_magic_link_end_to_end():
    client, captured = _client_with_capture()
    r = client.post("/auth/magic-link", json={"email": "alice@example.com"})
    assert r.status_code == 202
    assert len(captured.sent) == 1
    body = captured.sent[0]["body"]
    assert "/auth/verify?token=" in body
    token = _extract_token(body)

    # Verify mints a session cookie + creates the user.
    r = client.get(f"/auth/verify?token={token}")
    assert r.status_code == 200, r.text
    settings = get_settings()
    assert settings.session_cookie_name in r.cookies
    user_id = r.json()["user_id"]
    assert r.json()["created"] is True

    # /me returns the user.
    r = client.get("/auth/me")
    assert r.status_code == 200
    assert r.json()["id"] == user_id
    assert r.json()["email"] == "alice@example.com"


async def test_magic_link_token_is_single_use():
    client, captured = _client_with_capture()
    client.post("/auth/magic-link", json={"email": "bob@example.com"})
    token = _extract_token(captured.sent[0]["body"])

    r1 = client.get(f"/auth/verify?token={token}")
    assert r1.status_code == 200

    # Second use should fail.
    r2 = client.get(f"/auth/verify?token={token}", cookies={})
    assert r2.status_code == 400
    assert "already used" in r2.json()["detail"]


async def test_magic_link_token_rejected_when_invalid():
    client, _ = _client_with_capture()
    r = client.get("/auth/verify?token=nonsense-not-a-real-token")
    assert r.status_code == 400


async def test_me_requires_auth():
    client, _ = _client_with_capture()
    r = client.get("/auth/me")
    assert r.status_code == 401


async def test_logout_clears_cookie():
    client, captured = _client_with_capture()
    client.post("/auth/magic-link", json={"email": "carol@example.com"})
    token = _extract_token(captured.sent[0]["body"])
    client.get(f"/auth/verify?token={token}")

    r = client.post("/auth/logout")
    assert r.status_code == 204
    # After logout the cookie is cleared; /me must 401.
    client.cookies.clear()
    r = client.get("/auth/me")
    assert r.status_code == 401


async def test_allowlisted_address_still_gets_a_link(monkeypatch):
    """The allowlist must not disturb the happy path for a listed address."""
    monkeypatch.setenv(
        "WOLFPAW_ALLOWED_EMAILS", "alice@example.com,rory@example.com"
    )
    get_settings.cache_clear()  # type: ignore[attr-defined]

    client, captured = _client_with_capture()
    r = client.post("/auth/magic-link", json={"email": "rory@example.com"})
    assert r.status_code == 202
    assert len(captured.sent) == 1

    token = _extract_token(captured.sent[0]["body"])
    r = client.get(f"/auth/verify?token={token}")
    assert r.status_code == 200, r.text
    assert r.json()["created"] is True


async def test_verify_rejects_token_for_address_dropped_from_allowlist(
    monkeypatch,
):
    """A link minted while allowed must stop working once the address is
    removed — otherwise revoking access wouldn't take effect until the
    outstanding token expired."""
    monkeypatch.setenv("WOLFPAW_ALLOWED_EMAILS", "eve@example.com")
    get_settings.cache_clear()  # type: ignore[attr-defined]

    client, captured = _client_with_capture()
    client.post("/auth/magic-link", json={"email": "eve@example.com"})
    token = _extract_token(captured.sent[0]["body"])

    # Access revoked before she clicks the link.
    monkeypatch.setenv("WOLFPAW_ALLOWED_EMAILS", "alice@example.com")
    get_settings.cache_clear()  # type: ignore[attr-defined]

    r = client.get(f"/auth/verify?token={token}")
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid token"


async def test_default_subscription_seeded_for_new_user():
    """New users land with subscriptions.tier = 'dev' and a user_profiles row."""
    client, captured = _client_with_capture()
    client.post("/auth/magic-link", json={"email": "dora@example.com"})
    token = _extract_token(captured.sent[0]["body"])
    r = client.get(f"/auth/verify?token={token}")
    user_id = r.json()["user_id"]

    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    conn = await asyncpg.connect(dsn=dsn)
    try:
        tier = await conn.fetchval(
            "SELECT tier::text FROM subscriptions WHERE user_id = $1", user_id
        )
        profile = await conn.fetchval(
            "SELECT version FROM user_profiles WHERE user_id = $1", user_id
        )
    finally:
        await conn.close()
    assert tier == "dev"
    assert profile == 1
