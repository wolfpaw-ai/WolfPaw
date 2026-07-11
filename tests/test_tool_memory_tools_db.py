"""DB-backed tests for the `recall_memory` tool.

Uses the stub embedder (deterministic per-text vectors: identical text →
distance 0, unrelated text → ~orthogonal, distance ~1) so the relevance
floor cleanly separates on-subject hits from filler. Skipped without
WOLFPAW_TEST_DATABASE_URL.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import asyncpg
import pytest

from wolfpaw.memory.db import apply_sql_file, close_pool, migrations_dir
from wolfpaw.toolbox.registry import ToolContext, get_registry

pytestmark = pytest.mark.skipif(
    not os.getenv("WOLFPAW_TEST_DATABASE_URL"),
    reason="WOLFPAW_TEST_DATABASE_URL not set",
)


@pytest.fixture(autouse=True)
async def _fresh_db(monkeypatch):
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    monkeypatch.setenv("WOLFPAW_DATABASE_URL", dsn)
    monkeypatch.setenv("WOLFPAW_EMBEDDING_BACKEND", "stub")
    monkeypatch.setenv("WOLFPAW_WORKERS_ENABLED", "false")
    from wolfpaw.config import get_settings
    from wolfpaw.embeddings import reset_embedder

    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_embedder()
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        for f in (
            "001_init.sql", "002_auth.sql",
            "008_conv_compaction.sql", "017_l3_digest.sql",
        ):
            await apply_sql_file(conn, migrations_dir() / f)
    finally:
        await conn.close()
    await close_pool()
    yield
    await close_pool()
    reset_embedder()


async def _seed_user() -> UUID:
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    conn = await asyncpg.connect(dsn=dsn)
    try:
        return await conn.fetchval(
            "INSERT INTO users (email, email_verified) VALUES ($1, TRUE)"
            " RETURNING id", f"mem+{uuid4().hex[:8]}@test.local",
        )
    finally:
        await conn.close()


async def _seed_thread(user_id: UUID) -> UUID:
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    conn = await asyncpg.connect(dsn=dsn)
    try:
        return await conn.fetchval(
            "INSERT INTO threads (user_id, channel) VALUES ($1, 'web')"
            " RETURNING id", user_id,
        )
    finally:
        await conn.close()


async def _seed_msg(thread_id: UUID, content: str, ts: datetime) -> UUID:
    from pgvector.asyncpg import register_vector

    from wolfpaw.embeddings import get_embedder

    vec = (await get_embedder().embed_one(content)).vectors[0]
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await register_vector(conn)
        mid = await conn.fetchval(
            "INSERT INTO messages (thread_id, role, content, created_at)"
            " VALUES ($1, 'user', $2, $3) RETURNING id",
            thread_id, content, ts,
        )
        await conn.execute(
            "INSERT INTO message_embeddings (message_id, embedding)"
            " VALUES ($1, $2)", mid, vec,
        )
        return mid
    finally:
        await conn.close()


def _ctx(user_id: UUID) -> ToolContext:
    return ToolContext(user_id=user_id)


# --- recall_memory ---------------------------------------------------------


async def test_recall_surfaces_old_on_topic_message():
    uid = await _seed_user()
    tid = await _seed_thread(uid)
    base = datetime.now(timezone.utc) - timedelta(days=30)
    # Oldest message is the distinctive one; 24 fillers after it push it
    # well outside the recent window.
    await _seed_msg(tid, "quantum widget calibration protocol", base)
    for i in range(24):
        await _seed_msg(tid, f"routine filler note {i}",
                        base + timedelta(minutes=i + 1))

    tool = get_registry().get("recall_memory")
    out = await tool.run(_ctx(uid), query="quantum widget calibration protocol")

    contents = [r["content"] for r in out["results"]]
    assert "quantum widget calibration protocol" in contents
    # The relevance floor keeps filler out of the *hits* (it may still ride
    # along as a neighbor window, but the distinctive hit must be present).
    assert out["result_count"] >= 1


async def test_recall_empty_when_no_thread():
    uid = await _seed_user()  # no thread/messages
    tool = get_registry().get("recall_memory")
    out = await tool.run(_ctx(uid), query="anything")
    assert out["result_count"] == 0


async def test_recall_spans_all_user_threads():
    """Deep recall reaches into an older, non-current thread too."""
    uid = await _seed_user()
    thread_a = await _seed_thread(uid)
    thread_b = await _seed_thread(uid)  # the "current"/newer thread
    base = datetime.now(timezone.utc) - timedelta(days=40)
    await _seed_msg(thread_a, "the mriswith migration plan", base)
    await _seed_msg(thread_b, "totally different recent topic",
                    datetime.now(timezone.utc) - timedelta(minutes=1))

    tool = get_registry().get("recall_memory")
    out = await tool.run(_ctx(uid), query="the mriswith migration plan")
    contents = [r["content"] for r in out["results"]]
    assert "the mriswith migration plan" in contents  # found in thread A
