"""DB-backed tests for the `recall_memory` and `delete_memories` tools.

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


async def _count_messages(thread_id: UUID) -> int:
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    conn = await asyncpg.connect(dsn=dsn)
    try:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM messages WHERE thread_id = $1", thread_id,
        )
    finally:
        await conn.close()


def _ctx(user_id: UUID, *, task: bool = False) -> ToolContext:
    return ToolContext(user_id=user_id, task_id=uuid4() if task else None)


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


# --- delete_memories -------------------------------------------------------


def _stub_ask_user(monkeypatch, answers: list[str]):
    """Replace the shared ask_user tool's run with one that returns the
    given answers in sequence."""
    it = iter(answers)

    async def fake_run(ctx, **kwargs):
        return {"question_id": str(uuid4()), "answer": next(it)}

    monkeypatch.setattr(get_registry().get("ask_user"), "run", fake_run)


async def test_delete_removes_confirmed_episode(monkeypatch):
    uid = await _seed_user()
    tid = await _seed_thread(uid)
    base = datetime.now(timezone.utc) - timedelta(days=10)
    # One on-subject cluster (3 msgs within minutes) + unrelated filler.
    falcon_ids = []
    for i in range(3):
        falcon_ids.append(await _seed_msg(
            tid, "project falcon launch schedule",
            base + timedelta(minutes=i)))
    for i in range(5):
        await _seed_msg(tid, f"unrelated grocery list item {i}",
                        base + timedelta(hours=3, minutes=i))

    before = await _count_messages(tid)
    _stub_ask_user(monkeypatch, ["1", "yes"])  # select episode 1, confirm

    # Stub embedder matches on exact text, so query the seeded phrase.
    tool = get_registry().get("delete_memories")
    out = await tool.run(
        _ctx(uid, task=True), subject="project falcon launch schedule",
    )

    assert out["deleted"] == 3
    assert await _count_messages(tid) == before - 3
    # The falcon messages are gone; the filler remains.
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    conn = await asyncpg.connect(dsn=dsn)
    try:
        surviving = await conn.fetchval(
            "SELECT COUNT(*) FROM messages WHERE id = ANY($1::uuid[])",
            falcon_ids,
        )
    finally:
        await conn.close()
    assert surviving == 0


async def test_delete_spans_all_user_threads(monkeypatch):
    """A copy of the subject in a *second* thread (e.g. a legacy per-channel
    split or a post-/reset thread) must also be deleted — memory is
    user-scoped, not thread-scoped."""
    uid = await _seed_user()
    thread_a = await _seed_thread(uid)
    thread_b = await _seed_thread(uid)
    base = datetime.now(timezone.utc) - timedelta(days=5)
    a_ids = [
        await _seed_msg(thread_a, "project falcon launch schedule", base),
        await _seed_msg(thread_a, "project falcon launch schedule",
                        base + timedelta(minutes=1)),
    ]
    b_id = await _seed_msg(thread_b, "project falcon launch schedule",
                           base + timedelta(days=1))
    await _seed_msg(thread_a, "unrelated chatter", base + timedelta(hours=5))

    _stub_ask_user(monkeypatch, ["all", "yes"])
    tool = get_registry().get("delete_memories")
    out = await tool.run(
        _ctx(uid, task=True), subject="project falcon launch schedule",
    )

    assert out["deleted"] == 3  # 2 from thread A + 1 from thread B
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    conn = await asyncpg.connect(dsn=dsn)
    try:
        surviving = await conn.fetchval(
            "SELECT COUNT(*) FROM messages WHERE id = ANY($1::uuid[])",
            a_ids + [b_id],
        )
    finally:
        await conn.close()
    assert surviving == 0


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


async def test_delete_cancelled_leaves_everything(monkeypatch):
    uid = await _seed_user()
    tid = await _seed_thread(uid)
    base = datetime.now(timezone.utc) - timedelta(days=3)
    for i in range(3):
        await _seed_msg(tid, "project falcon launch schedule",
                        base + timedelta(minutes=i))
    before = await _count_messages(tid)
    _stub_ask_user(monkeypatch, ["none"])  # cancel at selection

    tool = get_registry().get("delete_memories")
    out = await tool.run(
        _ctx(uid, task=True), subject="project falcon launch schedule",
    )

    assert out["deleted"] == 0
    assert out.get("cancelled") is True
    assert await _count_messages(tid) == before


async def test_delete_requires_task_context():
    uid = await _seed_user()
    from wolfpaw.toolbox.registry import ToolError

    tool = get_registry().get("delete_memories")
    with pytest.raises(ToolError):
        await tool.run(_ctx(uid, task=False), subject="anything")


async def test_delete_scrubs_summaries_touching_old_history(monkeypatch):
    uid = await _seed_user()
    tid = await _seed_thread(uid)
    base = datetime.now(timezone.utc) - timedelta(days=20)
    # Distinctive OLD message + 24 fillers so the old one sits outside the
    # recent window (summarized territory).
    old_id = await _seed_msg(tid, "project falcon secret codename", base)
    for i in range(24):
        await _seed_msg(tid, f"filler {i}", base + timedelta(minutes=i + 1))

    # A summary row exists (as compaction would have produced).
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await conn.execute(
            "INSERT INTO thread_summaries (thread_id, level, summary_md)"
            " VALUES ($1, 1, 'summary mentioning falcon')", tid,
        )
    finally:
        await conn.close()

    enqueued: list[UUID] = []

    async def fake_enqueue(thread_id):
        enqueued.append(thread_id)

    monkeypatch.setattr(
        "wolfpaw.workers.queue.enqueue_compact_thread", fake_enqueue,
    )
    _stub_ask_user(monkeypatch, ["1", "yes"])

    tool = get_registry().get("delete_memories")
    out = await tool.run(_ctx(uid, task=True), subject="project falcon secret codename")

    assert out["deleted"] >= 1
    assert out["summaries_rebuilding"] is True
    assert enqueued == [tid]
    # Summaries were cleared (they blended the now-deleted content).
    conn = await asyncpg.connect(dsn=dsn)
    try:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM thread_summaries WHERE thread_id = $1", tid,
        )
    finally:
        await conn.close()
    assert n == 0
    _ = old_id
