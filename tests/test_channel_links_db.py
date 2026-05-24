"""DB-backed tests for `memory.channel_links` + `channels.telegram_tokens`."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import pytest

from wolfpaw.channels.telegram_tokens import (
    LinkTokenError,
    consume,
    issue,
)
from wolfpaw.memory import channel_links
from wolfpaw.memory.db import apply_sql_file, close_pool, migrations_dir

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
            "006_telegram.sql",
        ):
            await apply_sql_file(conn, migrations_dir() / f)
    finally:
        await conn.close()
    await close_pool()
    yield
    await close_pool()


async def _conn():
    return await asyncpg.connect(dsn=os.environ["WOLFPAW_TEST_DATABASE_URL"])


async def _seed_user(dsn: str, *, email_suffix: str = "") -> UUID:
    conn = await asyncpg.connect(dsn=dsn)
    try:
        return await conn.fetchval(
            "INSERT INTO users (email, email_verified) VALUES ($1, TRUE)"
            " RETURNING id",
            f"link{email_suffix}+{uuid4().hex[:6]}@test.local",
        )
    finally:
        await conn.close()


# --- channel_links DAO ---------------------------------------------------


async def test_create_and_find_user():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        link = await channel_links.create(
            conn, user_id=uid, channel="telegram",
            external_id="11111", external_username="alice",
        )
        found = await channel_links.find_user(
            conn, channel="telegram", external_id="11111",
        )
    finally:
        await conn.close()
    assert link.user_id == uid
    assert found == uid


async def test_find_user_returns_none_for_unknown():
    conn = await _conn()
    try:
        result = await channel_links.find_user(
            conn, channel="telegram", external_id="not-linked",
        )
    finally:
        await conn.close()
    assert result is None


async def test_unique_violation_when_external_id_reused():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    alice = await _seed_user(dsn, email_suffix="a")
    bob = await _seed_user(dsn, email_suffix="b")
    conn = await _conn()
    try:
        await channel_links.create(
            conn, user_id=alice, channel="telegram", external_id="shared",
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await channel_links.create(
                conn, user_id=bob, channel="telegram", external_id="shared",
            )
    finally:
        await conn.close()


async def test_list_for_user_returns_only_owned():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    alice = await _seed_user(dsn, email_suffix="a")
    bob = await _seed_user(dsn, email_suffix="b")
    conn = await _conn()
    try:
        await channel_links.create(
            conn, user_id=alice, channel="telegram", external_id="A1",
        )
        await channel_links.create(
            conn, user_id=bob, channel="telegram", external_id="B1",
        )
        alice_links = await channel_links.list_for_user(conn, user_id=alice)
    finally:
        await conn.close()
    assert len(alice_links) == 1
    assert alice_links[0].external_id == "A1"


async def test_delete_scoped_to_user_and_channel():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        await channel_links.create(
            conn, user_id=uid, channel="telegram", external_id="T",
        )
        deleted = await channel_links.delete(
            conn, user_id=uid, channel="telegram",
        )
        remaining = await channel_links.list_for_user(conn, user_id=uid)
    finally:
        await conn.close()
    assert deleted == 1
    assert remaining == []


# --- link tokens ---------------------------------------------------------


async def test_issue_then_consume_returns_user_id():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        plain = await issue(
            conn, user_id=uid, channel="telegram", ttl_minutes=15,
        )
        consumed = await consume(conn, plain=plain, channel="telegram")
    finally:
        await conn.close()
    assert consumed == uid
    # The plain token is in URL-safe form, not a hex hash.
    assert len(plain) > 30


async def test_consume_rejects_unknown_token():
    conn = await _conn()
    try:
        with pytest.raises(LinkTokenError, match="invalid"):
            await consume(conn, plain="totally-fake", channel="telegram")
    finally:
        await conn.close()


async def test_consume_rejects_after_first_use():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        plain = await issue(
            conn, user_id=uid, channel="telegram", ttl_minutes=15,
        )
        await consume(conn, plain=plain, channel="telegram")
        with pytest.raises(LinkTokenError, match="already used"):
            await consume(conn, plain=plain, channel="telegram")
    finally:
        await conn.close()


async def test_consume_rejects_expired_token():
    """ttl_minutes=0 means already expired."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        plain = await issue(
            conn, user_id=uid, channel="telegram", ttl_minutes=0,
        )
        # Wait a tick so expires_at is definitely past.
        import asyncio
        await asyncio.sleep(0.05)
        with pytest.raises(LinkTokenError, match="expired"):
            await consume(conn, plain=plain, channel="telegram")
    finally:
        await conn.close()


async def test_consume_rejects_wrong_channel():
    """Token bound to channel A can't be used for channel B."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        plain = await issue(
            conn, user_id=uid, channel="telegram", ttl_minutes=15,
        )
        with pytest.raises(LinkTokenError, match="different channel"):
            await consume(conn, plain=plain, channel="slack")
    finally:
        await conn.close()
