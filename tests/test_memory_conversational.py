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
        for f in (
            "001_init.sql", "002_auth.sql", "003_sandbox.sql",
            "005_post_evaluator.sql", "008_conv_compaction.sql",
        ):
            await apply_sql_file(conn, migrations_dir() / f)
    finally:
        await conn.close()
    await close_pool()
    # The bare-DAO tests in this module don't want the embed + compact
    # follow-up firing under their feet. Step-22-specific tests below
    # opt back in via `enable_post_append_for_test` inside the test body.
    conv.disable_post_append_for_test()
    yield
    conv.enable_post_append_for_test()
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


async def test_get_or_create_thread_stamps_persona_versions():
    """Step 17 — new threads get soul_version + user_profile_version
    stamped so procedural-memory retrieval can scope by persona snapshot."""
    from wolfpaw.persona.soul import reset_soul, set_soul_for_test

    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    # Seed a profile so the version isn't NULL.
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await conn.execute(
            "INSERT INTO user_profiles (user_id, version) VALUES ($1, 3)",
            uid,
        )
    finally:
        await conn.close()

    set_soul_for_test("test-soul")
    try:
        tid = await _with_conn(
            conv.get_or_create_thread, user_id=uid, channel="web",
        )
        conn = await asyncpg.connect(dsn=dsn)
        try:
            row = await conn.fetchrow(
                "SELECT soul_version, user_profile_version"
                "  FROM threads WHERE id = $1",
                tid,
            )
        finally:
            await conn.close()
    finally:
        reset_soul()
    assert row["soul_version"] is not None
    assert len(row["soul_version"]) == 12
    assert row["user_profile_version"] == 3


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


# --- step 22: tiered memory ------------------------------------------------


async def test_fetch_summaries_returns_empty_for_fresh_thread():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    tid = await _with_conn(conv.get_or_create_thread, user_id=uid, channel="web")
    summaries = await _with_conn(conv.fetch_summaries, thread_id=tid)
    assert summaries == []


async def test_fetch_summaries_returns_unfolded_l1_only():
    """L1s without a folded_into_summary_id show up; folded L1s drop out
    in favour of the L2 that covers them."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    tid = await _with_conn(conv.get_or_create_thread, user_id=uid, channel="web")

    conn = await asyncpg.connect(dsn=dsn)
    try:
        # Two L1s, then an L2 that folds the first L1 (sets
        # folded_into_summary_id on it).
        l1a = await conn.fetchval(
            "INSERT INTO thread_summaries (thread_id, level, summary_md)"
            " VALUES ($1, 1, $2) RETURNING id",
            tid, "L1 alpha",
        )
        l1b = await conn.fetchval(
            "INSERT INTO thread_summaries (thread_id, level, summary_md)"
            " VALUES ($1, 1, $2) RETURNING id",
            tid, "L1 beta",
        )
        l2 = await conn.fetchval(
            "INSERT INTO thread_summaries (thread_id, level, summary_md)"
            " VALUES ($1, 2, $2) RETURNING id",
            tid, "L2 covering alpha",
        )
        await conn.execute(
            "UPDATE thread_summaries SET folded_into_summary_id = $1"
            " WHERE id = $2",
            l2, l1a,
        )
    finally:
        await conn.close()

    summaries = await _with_conn(conv.fetch_summaries, thread_id=tid)
    # L1 alpha (folded) drops out; L1 beta (un-folded) stays; L2 stays.
    contents = {s.summary_md for s in summaries}
    assert contents == {"L1 beta", "L2 covering alpha"}
    # Ordering is oldest-range-first: the L2 (which covers the older
    # "alpha" range) precedes the newer un-folded L1. fetch_summaries
    # orders by level DESC then created_at ASC, since higher levels
    # always cover strictly older content than surviving lower ones.
    assert [s.summary_md for s in summaries] == ["L2 covering alpha", "L1 beta"]


async def _seed_msg_with_embedding(
    dsn: str, thread_id: UUID, content: str,
    *, role: str = "user", created_at=None, embedder=None,
) -> UUID:
    """Direct INSERT bypassing conv.append so we can pin timestamps and
    write the embedding in one transaction (no background tasks)."""
    from pgvector.asyncpg import register_vector

    conn = await asyncpg.connect(dsn=dsn)
    try:
        await register_vector(conn)
        if created_at is None:
            msg_id = await conn.fetchval(
                "INSERT INTO messages (thread_id, role, content)"
                " VALUES ($1, $2::message_role, $3) RETURNING id",
                thread_id, role, content,
            )
        else:
            msg_id = await conn.fetchval(
                "INSERT INTO messages (thread_id, role, content, created_at)"
                " VALUES ($1, $2::message_role, $3, $4) RETURNING id",
                thread_id, role, content, created_at,
            )
        if embedder is not None:
            vec = (await embedder.embed_one(content)).vectors[0]
            await conn.execute(
                "INSERT INTO message_embeddings (message_id, embedding)"
                " VALUES ($1, $2)",
                msg_id, vec,
            )
        return msg_id
    finally:
        await conn.close()


async def test_search_relevant_excludes_recent_window():
    """The newest exclude_recent_n messages must NOT appear in the
    semantic-recall result — the Planner already pulled them via
    fetch_recent and would otherwise see duplicates."""
    from datetime import datetime, timedelta, timezone

    from wolfpaw.embeddings.stub import StubEmbedder

    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    tid = await _with_conn(conv.get_or_create_thread, user_id=uid, channel="web")

    embedder = StubEmbedder(dimensions=1024)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    for i, c in enumerate(["alpha", "beta", "gamma"]):
        await _seed_msg_with_embedding(
            dsn, tid, c, created_at=base + timedelta(seconds=i), embedder=embedder,
        )

    # Query against alpha's vector. With exclude_recent_n=2, beta and
    # gamma (the two newest) are excluded; alpha is the only candidate.
    query_vec = (await embedder.embed_one("alpha")).vectors[0]
    hits = await _with_conn(
        conv.search_relevant,
        thread_id=tid, query_embedding=query_vec, k=5, exclude_recent_n=2,
    )
    assert [h.content for h in hits] == ["alpha"]


async def test_search_relevant_ranks_by_cosine_similarity():
    """An exact-vector query should return the matching message first
    (with exclude_recent_n=0 so nothing is filtered out)."""
    from wolfpaw.embeddings.stub import StubEmbedder

    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    tid = await _with_conn(conv.get_or_create_thread, user_id=uid, channel="web")

    embedder = StubEmbedder(dimensions=1024)
    for c in ["apples and pears", "freight trains", "orchard fruit"]:
        await _seed_msg_with_embedding(dsn, tid, c, embedder=embedder)

    query_vec = (await embedder.embed_one("apples and pears")).vectors[0]
    hits = await _with_conn(
        conv.search_relevant,
        thread_id=tid, query_embedding=query_vec, k=3, exclude_recent_n=0,
    )
    assert hits
    assert hits[0].content == "apples and pears"


async def test_search_relevant_isolates_per_thread():
    """A semantic match in thread A must not surface when searching
    thread B."""
    from wolfpaw.embeddings.stub import StubEmbedder

    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    a = await _with_conn(conv.get_or_create_thread, user_id=uid, channel="web")
    b = await _with_conn(conv.get_or_create_thread, user_id=uid, channel="web")
    embedder = StubEmbedder(dimensions=1024)

    await _seed_msg_with_embedding(dsn, a, "in A", embedder=embedder)
    await _seed_msg_with_embedding(dsn, b, "in B", embedder=embedder)

    query = (await embedder.embed_one("in A")).vectors[0]
    hits = await _with_conn(
        conv.search_relevant,
        thread_id=b, query_embedding=query, k=5, exclude_recent_n=0,
    )
    assert [h.content for h in hits] == ["in B"]


