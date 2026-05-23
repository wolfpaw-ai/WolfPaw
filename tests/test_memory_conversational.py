"""DB-backed tests for `memory.conversational` — thread creation, append,
and the fetch_recent contract.

Skipped without WOLFPAW_TEST_DATABASE_URL."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import pytest

from wolfpaw.memory import conversational as conv
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
        for f in ("001_init.sql", "002_auth.sql", "003_sandbox.sql"):
            await apply_sql_file(conn, migrations_dir() / f)
    finally:
        await conn.close()
    await close_pool()
    yield
    await close_pool()


async def _seed_user(dsn: str) -> UUID:
    conn = await asyncpg.connect(dsn=dsn)
    try:
        return await conn.fetchval(
            "INSERT INTO users (email, email_verified) VALUES ($1, TRUE)"
            " RETURNING id",
            f"conv+{uuid4().hex[:8]}@test.local",
        )
    finally:
        await conn.close()


async def _with_conn(fn, *args, **kwargs):
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    conn = await asyncpg.connect(dsn=dsn)
    try:
        return await fn(conn, *args, **kwargs)
    finally:
        await conn.close()


async def test_get_or_create_thread_creates_when_missing():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    tid = await _with_conn(
        conv.get_or_create_thread, user_id=uid, channel="web", thread_id=None,
    )
    assert isinstance(tid, UUID)


async def test_get_or_create_thread_returns_existing_when_owner_matches():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    first = await _with_conn(
        conv.get_or_create_thread, user_id=uid, channel="web",
    )
    second = await _with_conn(
        conv.get_or_create_thread, user_id=uid, channel="web", thread_id=first,
    )
    assert first == second


async def test_get_or_create_thread_silently_replaces_cross_user_id():
    """Passing another user's thread_id must NOT leak it back — we create
    a fresh thread instead. (Avoids exposing thread-id existence.)"""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    alice = await _seed_user(dsn)
    bob = await _seed_user(dsn)
    alice_thread = await _with_conn(
        conv.get_or_create_thread, user_id=alice, channel="web",
    )
    bob_thread = await _with_conn(
        conv.get_or_create_thread,
        user_id=bob, channel="web", thread_id=alice_thread,
    )
    assert bob_thread != alice_thread


async def test_append_and_fetch_recent_returns_chronological():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    tid = await _with_conn(conv.get_or_create_thread, user_id=uid, channel="web")
    for role, text in [
        ("user", "hi"),
        ("assistant", "hello"),
        ("user", "what's 2+2?"),
        ("assistant", "4"),
    ]:
        await _with_conn(
            conv.append, thread_id=tid, role=role, content=text,
        )
    msgs = await _with_conn(conv.fetch_recent, thread_id=tid, n=20)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "hi"), ("assistant", "hello"),
        ("user", "what's 2+2?"), ("assistant", "4"),
    ]


async def test_fetch_recent_honors_n_cap_keeping_newest():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    tid = await _with_conn(conv.get_or_create_thread, user_id=uid, channel="web")
    for i in range(5):
        await _with_conn(
            conv.append, thread_id=tid, role="user", content=f"msg-{i}",
        )
    last2 = await _with_conn(conv.fetch_recent, thread_id=tid, n=2)
    assert [m.content for m in last2] == ["msg-3", "msg-4"]


async def test_fetch_recent_isolates_per_thread():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    a = await _with_conn(conv.get_or_create_thread, user_id=uid, channel="web")
    b = await _with_conn(conv.get_or_create_thread, user_id=uid, channel="web")
    await _with_conn(conv.append, thread_id=a, role="user", content="in A")
    await _with_conn(conv.append, thread_id=b, role="user", content="in B")
    assert [m.content for m in await _with_conn(conv.fetch_recent, thread_id=a, n=20)] == ["in A"]
    assert [m.content for m in await _with_conn(conv.fetch_recent, thread_id=b, n=20)] == ["in B"]
