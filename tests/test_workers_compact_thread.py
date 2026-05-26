"""DB-backed tests for the thread compaction worker.

Uses an injected `set_summarizer_for_test` summarizer so we exercise
the L1 / L2 folding logic without making real Anthropic calls.

Skipped without WOLFPAW_TEST_DATABASE_URL."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import pytest

from wolfpaw.memory import conversational as conv
from wolfpaw.memory.db import apply_sql_file, close_pool, migrations_dir
from wolfpaw.workers.jobs.compact_thread import (
    compact_thread,
    set_summarizer_for_test,
)

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
            "005_post_evaluator.sql", "008_conv_compaction.sql",
        ):
            await apply_sql_file(conn, migrations_dir() / f)
    finally:
        await conn.close()
    await close_pool()
    conv.disable_post_append_for_test()  # we drive append manually
    yield
    set_summarizer_for_test(None)
    conv.enable_post_append_for_test()
    await close_pool()


async def _seed_user(dsn: str) -> UUID:
    conn = await asyncpg.connect(dsn=dsn)
    try:
        return await conn.fetchval(
            "INSERT INTO users (email, email_verified) VALUES ($1, TRUE)"
            " RETURNING id",
            f"compactor+{uuid4().hex[:8]}@test.local",
        )
    finally:
        await conn.close()


async def _seed_thread(dsn: str, user_id: UUID) -> UUID:
    conn = await asyncpg.connect(dsn=dsn)
    try:
        return await conn.fetchval(
            "INSERT INTO threads (user_id, channel) VALUES ($1, 'web')"
            " RETURNING id",
            user_id,
        )
    finally:
        await conn.close()


async def _append_n(thread_id: UUID, n: int) -> None:
    conn = await asyncpg.connect(dsn=os.environ["WOLFPAW_TEST_DATABASE_URL"])
    try:
        for i in range(n):
            await conn.execute(
                "INSERT INTO messages (thread_id, role, content)"
                " VALUES ($1, 'user', $2)",
                thread_id, f"message-{i}",
            )
    finally:
        await conn.close()


async def test_compact_thread_noop_below_trigger_threshold():
    """A thread with fewer than `compaction_trigger_threshold` messages
    must not produce any summary rows."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    tid = await _seed_thread(dsn, uid)
    await _append_n(tid, 20)  # below the 40-default trigger

    called = []

    async def summarizer(items, _user_id):
        called.append(items)
        return "should not be called"

    set_summarizer_for_test(summarizer)
    await compact_thread(tid)

    conn = await asyncpg.connect(dsn=dsn)
    try:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM thread_summaries WHERE thread_id = $1",
            tid,
        )
    finally:
        await conn.close()
    assert count == 0
    assert called == []


async def test_compact_thread_creates_l1_at_trigger_threshold():
    """With 40 messages and a 20-message recent window, the compactor
    folds the oldest 20 into one L1 row."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    tid = await _seed_thread(dsn, uid)
    await _append_n(tid, 40)

    seen_payloads: list = []

    async def summarizer(items, _user_id):
        seen_payloads.append(items)
        return "summary of 20 turns"

    set_summarizer_for_test(summarizer)
    await compact_thread(tid)

    conn = await asyncpg.connect(dsn=dsn)
    try:
        rows = await conn.fetch(
            "SELECT level, summary_md, range_start_message_id,"
            "       range_end_message_id"
            "  FROM thread_summaries WHERE thread_id = $1"
            " ORDER BY created_at ASC, id ASC",
            tid,
        )
    finally:
        await conn.close()
    assert len(rows) == 1
    assert rows[0]["level"] == 1
    assert rows[0]["summary_md"] == "summary of 20 turns"
    assert rows[0]["range_start_message_id"] is not None
    assert rows[0]["range_end_message_id"] is not None
    assert len(seen_payloads) == 1
    assert len(seen_payloads[0]) == 20  # exactly the L1 window size


async def test_compact_thread_drains_multiple_l1_rounds():
    """A larger backlog produces multiple L1 rows in one drain pass."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    tid = await _seed_thread(dsn, uid)
    # 80 messages = 60 to summarize past the 20-message recent window
    # = 3 L1 windows.
    await _append_n(tid, 80)

    async def summarizer(items, _user_id):
        return f"summary of {len(items)} turns"

    set_summarizer_for_test(summarizer)
    await compact_thread(tid)

    conn = await asyncpg.connect(dsn=dsn)
    try:
        rows = await conn.fetch(
            "SELECT level, summary_md FROM thread_summaries"
            " WHERE thread_id = $1 ORDER BY created_at ASC, id ASC",
            tid,
        )
    finally:
        await conn.close()
    assert len(rows) == 3
    assert all(r["level"] == 1 for r in rows)


async def test_compact_thread_folds_l1_into_l2_at_threshold():
    """Once 10 un-folded L1s exist, the compactor produces one L2 and
    stamps `folded_into_summary_id` on each child."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    tid = await _seed_thread(dsn, uid)

    # Insert 10 L1 rows directly so we don't need 220 fake messages.
    conn = await asyncpg.connect(dsn=dsn)
    try:
        for i in range(10):
            await conn.execute(
                "INSERT INTO thread_summaries"
                " (thread_id, level, summary_md)"
                " VALUES ($1, 1, $2)",
                tid, f"L1-{i}",
            )
    finally:
        await conn.close()

    async def summarizer(items, _user_id):
        # The L2 fold sends summary dicts, not raw messages.
        kinds = {it.get("kind") for it in items}
        assert kinds == {"level_1_summary"}
        return f"L2 fold of {len(items)} L1s"

    set_summarizer_for_test(summarizer)
    await compact_thread(tid)

    conn = await asyncpg.connect(dsn=dsn)
    try:
        l2_rows = await conn.fetch(
            "SELECT id, summary_md FROM thread_summaries"
            " WHERE thread_id = $1 AND level = 2",
            tid,
        )
        folded_count = await conn.fetchval(
            "SELECT COUNT(*) FROM thread_summaries"
            " WHERE thread_id = $1 AND level = 1"
            "   AND folded_into_summary_id IS NOT NULL",
            tid,
        )
    finally:
        await conn.close()
    assert len(l2_rows) == 1
    assert l2_rows[0]["summary_md"] == "L2 fold of 10 L1s"
    assert folded_count == 10


async def test_compact_thread_idempotent():
    """Running compact_thread twice in a row produces the same set of
    summaries as one invocation (the second pass finds nothing to do)."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    tid = await _seed_thread(dsn, uid)
    await _append_n(tid, 40)

    async def summarizer(items, _user_id):
        return "stable summary"

    set_summarizer_for_test(summarizer)
    await compact_thread(tid)
    await compact_thread(tid)

    conn = await asyncpg.connect(dsn=dsn)
    try:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM thread_summaries WHERE thread_id = $1",
            tid,
        )
    finally:
        await conn.close()
    assert count == 1


async def test_fetch_summaries_prefers_l2_after_fold():
    """After a real L1→L2 fold, fetch_summaries returns the L2 and the
    surviving (un-folded) L1s, never the folded L1s."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    tid = await _seed_thread(dsn, uid)

    conn = await asyncpg.connect(dsn=dsn)
    try:
        for i in range(11):
            await conn.execute(
                "INSERT INTO thread_summaries"
                " (thread_id, level, summary_md) VALUES ($1, 1, $2)",
                tid, f"L1-{i}",
            )
    finally:
        await conn.close()

    async def summarizer(items, _user_id):
        return "rolled up"

    set_summarizer_for_test(summarizer)
    await compact_thread(tid)

    conn = await asyncpg.connect(dsn=dsn)
    try:
        rows = await conv.fetch_summaries(conn, thread_id=tid)
    finally:
        await conn.close()
    # 10 L1s folded → 1 L2 visible; one L1 (the 11th) remains un-folded.
    levels = sorted(r.level for r in rows)
    contents = {r.summary_md for r in rows}
    assert levels == [1, 2]
    assert "rolled up" in contents
    assert "L1-10" in contents  # the 11th L1, un-folded
