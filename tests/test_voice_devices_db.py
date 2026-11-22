"""DB-backed tests for `memory.voice_devices` and the voice pairing code path.
Skipped unless WOLFPAW_TEST_DATABASE_URL points at a throwaway database."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import pytest

from wolfpaw.channels.telegram_tokens import LinkTokenError, _hash, consume
from wolfpaw.memory import channel_links, voice_devices
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
        for f in sorted(p.name for p in migrations_dir().glob("*.sql")):
            await apply_sql_file(conn, migrations_dir() / f)
    finally:
        await conn.close()
    await close_pool()
    yield
    await close_pool()


async def _conn():
    return await asyncpg.connect(dsn=os.environ["WOLFPAW_TEST_DATABASE_URL"])


async def _seed_user() -> UUID:
    conn = await _conn()
    try:
        return await conn.fetchval(
            "INSERT INTO users (email, email_verified) VALUES ($1, TRUE) RETURNING id",
            f"voice+{uuid4().hex[:6]}@test.local",
        )
    finally:
        await conn.close()


async def test_create_returns_a_working_token_and_a_channel_link():
    uid = await _seed_user()
    conn = await _conn()
    try:
        device, token = await voice_devices.create(conn, user_id=uid, name="Kitchen")
        found = await voice_devices.find_by_token(conn, plain=token)
        links = await channel_links.list_for_user(conn, user_id=uid)
        stored = await conn.fetchval("SELECT token_hash FROM voice_devices WHERE id = $1", device.id)
    finally:
        await conn.close()
    assert found is not None and found.id == device.id
    assert stored != token and stored == voice_devices.hash_token(token)  # only the hash is stored
    assert [(l.channel, l.external_id) for l in links] == [("voice", str(device.id))]


async def test_a_wrong_token_finds_nothing():
    conn = await _conn()
    try:
        assert await voice_devices.find_by_token(conn, plain="nope") is None
    finally:
        await conn.close()


async def test_a_user_can_pair_several_devices():
    uid = await _seed_user()
    conn = await _conn()
    try:
        await voice_devices.create(conn, user_id=uid, name="Kitchen")
        await voice_devices.create(conn, user_id=uid, name="Living room")
        devices = await voice_devices.list_for_user(conn, user_id=uid)
    finally:
        await conn.close()
    assert [d.name for d in devices] == ["Kitchen", "Living room"]


async def test_revoke_disables_the_token_and_drops_the_link():
    uid = await _seed_user()
    other = await _seed_user()
    conn = await _conn()
    try:
        device, token = await voice_devices.create(conn, user_id=uid, name="Kitchen")
        assert await voice_devices.revoke(conn, user_id=other, device_id=device.id) is False  # not theirs
        assert await voice_devices.revoke(conn, user_id=uid, device_id=device.id) is True
        assert await voice_devices.revoke(conn, user_id=uid, device_id=device.id) is False  # already revoked
        assert await voice_devices.find_by_token(conn, plain=token) is None
        assert await channel_links.list_for_user(conn, user_id=uid) == []
        assert await voice_devices.list_for_user(conn, user_id=uid) == []
    finally:
        await conn.close()


async def test_pairing_codes_work_once_and_only_for_voice():
    uid = await _seed_user()
    conn = await _conn()
    try:
        await conn.execute(
            "INSERT INTO channel_link_tokens (user_id, channel, token_hash, expires_at)"
            " VALUES ($1, 'voice'::channel, $2, NOW() + INTERVAL '5 minutes')",
            uid, _hash("ABCD2345"),
        )
        with pytest.raises(LinkTokenError):
            await consume(conn, plain="ABCD2345", channel="telegram")
        assert await consume(conn, plain="ABCD2345", channel="voice") == uid
        with pytest.raises(LinkTokenError):
            await consume(conn, plain="ABCD2345", channel="voice")
    finally:
        await conn.close()
