"""integration_links DAO against a real Postgres (skipped without
WOLFPAW_TEST_DATABASE_URL, like the other *_db tests)."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import pytest

from wolfpaw.memory import integration_links as dao

pytestmark = pytest.mark.skipif(
    not os.getenv("WOLFPAW_TEST_DATABASE_URL"),
    reason="WOLFPAW_TEST_DATABASE_URL not set",
)


@pytest.fixture
async def dsn(monkeypatch):
    from wolfpaw.config import get_settings
    from wolfpaw.memory.db import apply_sql_file, close_pool, migrations_dir

    url = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    monkeypatch.setenv("WOLFPAW_DATABASE_URL", url)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    await close_pool()
    conn = await asyncpg.connect(dsn=url)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        for f in ("001_init.sql", "002_auth.sql", "023_integration_links.sql"):
            await apply_sql_file(conn, migrations_dir() / f)
    finally:
        await conn.close()
    yield url
    await close_pool()


async def _seed_user(conn: asyncpg.Connection) -> UUID:
    return await conn.fetchval(
        "INSERT INTO users (email, email_verified) VALUES ($1, TRUE) RETURNING id",
        f"il+{uuid4().hex[:8]}@test.local",
    )


async def test_upsert_get_replace_delete(dsn):
    conn = await asyncpg.connect(dsn=dsn)
    try:
        uid = await _seed_user(conn)
        assert await dao.get(conn, user_id=uid, provider="p") is None

        await dao.upsert(conn, user_id=uid, provider="p", auth_kind="api_key",
                         credentials={"api_key": "one"}, scopes=["a"], display={"k": 1})
        link = await dao.get(conn, user_id=uid, provider="p")
        assert link.credentials == {"api_key": "one"}
        assert link.scopes == ["a"] and link.display == {"k": 1}
        assert "one" not in repr(link)  # credentials are never printed

        await dao.upsert(conn, user_id=uid, provider="p", auth_kind="api_key",
                         credentials={"api_key": "two"})
        assert (await dao.get(conn, user_id=uid, provider="p")).credentials == {"api_key": "two"}
        assert await dao.get(conn, user_id=uid, provider="other") is None  # providers independent

        assert await dao.delete(conn, user_id=uid, provider="p") is True
        assert await dao.delete(conn, user_id=uid, provider="p") is False
    finally:
        await conn.close()
