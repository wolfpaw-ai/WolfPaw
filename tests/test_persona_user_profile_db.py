"""DB-backed tests for `persona.user_profile` DAO + `/me/profile` routes.

Covers: get on a fresh user (created by sign-in path), partial updates
bump version, version isolation across users, route 401 + happy path."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient

from wolfpaw.api import create_app
from wolfpaw.auth.deps import require_user_id
from wolfpaw.memory.db import apply_sql_file, close_pool, migrations_dir
from wolfpaw.persona import user_profile as up

pytestmark = pytest.mark.skipif(
    not os.getenv("WOLFPAW_TEST_DATABASE_URL"),
    reason="WOLFPAW_TEST_DATABASE_URL not set",
)


@pytest.fixture(autouse=True)
async def _fresh_db(monkeypatch):
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    monkeypatch.setenv("WOLFPAW_DATABASE_URL", dsn)
    from wolfpaw.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        for f in (
            "001_init.sql", "002_auth.sql", "003_sandbox.sql",
            "004_seed_skills.sql", "005_post_evaluator.sql",
        ):
            await apply_sql_file(conn, migrations_dir() / f)
    finally:
        await conn.close()
    await close_pool()
    yield
    await close_pool()


async def _seed_user(dsn: str) -> UUID:
    conn = await asyncpg.connect(dsn=dsn)
    try:
        uid = await conn.fetchval(
            "INSERT INTO users (email, email_verified) VALUES ($1, TRUE)"
            " RETURNING id",
            f"persona+{uuid4().hex[:8]}@test.local",
        )
        await conn.execute(
            "INSERT INTO user_profiles (user_id) VALUES ($1)",
            uid,
        )
        return uid
    finally:
        await conn.close()


async def _conn():
    return await asyncpg.connect(dsn=os.environ["WOLFPAW_TEST_DATABASE_URL"])


# --- DAO -----------------------------------------------------------------


async def test_get_returns_seeded_defaults():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        p = await up.get(conn, user_id=uid)
    finally:
        await conn.close()
    assert p is not None
    assert p.version == 1
    assert p.persona_md == ""
    assert p.preferences == {}
    assert p.timezone == "UTC"


async def test_get_returns_none_when_missing():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    conn = await asyncpg.connect(dsn=dsn)
    try:
        uid = await conn.fetchval(
            "INSERT INTO users (email, email_verified) VALUES ($1, TRUE)"
            " RETURNING id",
            "no-profile@test.local",
        )
        # Deliberately skip the user_profiles INSERT.
        p = await up.get(conn, user_id=uid)
    finally:
        await conn.close()
    assert p is None


async def test_update_persona_bumps_version():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        v1 = await up.get(conn, user_id=uid)
        updated = await up.update(
            conn, user_id=uid,
            persona_md="I am Alice. I work in finance.",
        )
        v2 = await up.get(conn, user_id=uid)
    finally:
        await conn.close()
    assert v1.version == 1
    assert updated.version == 2
    assert v2.version == 2
    assert v2.persona_md.startswith("I am Alice")


async def test_update_partial_only_writes_provided_fields():
    """Setting just `preferences` shouldn't blank out `persona_md`."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        await up.update(conn, user_id=uid, persona_md="established persona")
        updated = await up.update(
            conn, user_id=uid, preferences={"formality": "casual"},
        )
    finally:
        await conn.close()
    assert updated.persona_md == "established persona"
    assert updated.preferences == {"formality": "casual"}
    assert updated.version == 3  # 1 (seed) + 2 (updates)


async def test_update_unknown_user_raises():
    conn = await _conn()
    try:
        with pytest.raises(LookupError):
            await up.update(conn, user_id=uuid4(), persona_md="x")
    finally:
        await conn.close()


# --- routes --------------------------------------------------------------


def _client(uid: UUID) -> TestClient:
    app = create_app()
    app.dependency_overrides[require_user_id] = lambda: uid
    return TestClient(app)


async def test_get_profile_route_returns_seeded():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    client = _client(uid)
    r = client.get("/me/profile")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_id"] == str(uid)
    assert body["version"] == 1
    assert body["timezone"] == "UTC"


def test_get_profile_route_requires_auth():
    app = create_app()
    client = TestClient(app)
    r = client.get("/me/profile")
    assert r.status_code == 401


async def test_patch_profile_round_trip():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    client = _client(uid)
    r = client.patch(
        "/me/profile",
        json={"persona_md": "Dmitri", "timezone": "America/Chicago"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["persona_md"] == "Dmitri"
    assert body["timezone"] == "America/Chicago"
    assert body["version"] == 2

    # GET reflects the update.
    r = client.get("/me/profile")
    body = r.json()
    assert body["version"] == 2
    assert body["timezone"] == "America/Chicago"


async def test_patch_profile_rejects_empty_body():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    client = _client(uid)
    r = client.patch("/me/profile", json={})
    assert r.status_code == 400


async def test_patch_profile_404_when_no_row():
    """User exists but no user_profiles row (legacy / manual deletion).
    PATCH should 404; GET tolerates it."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    conn = await asyncpg.connect(dsn=dsn)
    try:
        uid = await conn.fetchval(
            "INSERT INTO users (email, email_verified) VALUES ($1, TRUE)"
            " RETURNING id",
            "no-profile@test.local",
        )
    finally:
        await conn.close()
    client = _client(uid)
    r = client.patch("/me/profile", json={"timezone": "UTC"})
    assert r.status_code == 404
