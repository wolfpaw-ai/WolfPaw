"""Logout clears the session cookie.

No DB needed — `/auth/logout` only emits a cookie-clearing header.

Regression coverage for two bugs that made "Sign out" appear to work while
leaving the user signed in: the handler returned FastAPI's *injected*
`Response` (yielding a `None` status code), and `delete_cookie` was called
with its permissive defaults, so the clearing header didn't match the
`Secure; HttpOnly` cookie minted at verify time.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from wolfpaw.api import create_app
from wolfpaw.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()  # type: ignore[attr-defined]
    yield
    get_settings.cache_clear()  # type: ignore[attr-defined]


def _logout(monkeypatch, env: str):
    monkeypatch.setenv("WOLFPAW_ENV", env)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    return TestClient(create_app()).post("/auth/logout")


def test_logout_returns_204(monkeypatch):
    """A `None` status code here is what the injected-Response bug produced."""
    r = _logout(monkeypatch, "prod")
    assert r.status_code == 204


def test_logout_clears_the_session_cookie(monkeypatch):
    r = _logout(monkeypatch, "prod")
    cookie = r.headers["set-cookie"]
    assert get_settings().session_cookie_name in cookie
    assert "Max-Age=0" in cookie


def test_logout_cookie_attributes_match_the_minted_cookie(monkeypatch):
    """Browsers may refuse to overwrite a Secure/HttpOnly cookie with a
    header that doesn't carry the same attributes."""
    r = _logout(monkeypatch, "prod")
    cookie = r.headers["set-cookie"]
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "Path=/" in cookie
    assert "SameSite=lax" in cookie


def test_logout_omits_secure_outside_prod(monkeypatch):
    """`secure` tracks env the same way it does when the cookie is set —
    otherwise local dev over plain HTTP could never clear its cookie."""
    r = _logout(monkeypatch, "dev")
    cookie = r.headers["set-cookie"]
    assert "Secure" not in cookie
    assert "HttpOnly" in cookie
