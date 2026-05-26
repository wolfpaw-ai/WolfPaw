"""DB-backed tests for the `memory.tools` DAO (step 28).

Verifies the proposed → approved/rejected state transitions, the
``find_active_by_name`` dispatch lookup, the cosine search for dedup,
and the user-scoping invariants (one user's tools never surface for
another user).

Skipped without WOLFPAW_TEST_DATABASE_URL."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import pytest

from wolfpaw.embeddings.stub import StubEmbedder
from wolfpaw.memory import tools as tools_dao
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
            "008_conv_compaction.sql", "009_skill_distiller.sql",
            "010_skill_supersession.sql", "011_tool_creator.sql",
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
        return await conn.fetchval(
            "INSERT INTO users (email, email_verified) VALUES ($1, TRUE)"
            " RETURNING id",
            f"tools+{uuid4().hex[:8]}@test.local",
        )
    finally:
        await conn.close()


async def _conn():
    return await asyncpg.connect(dsn=os.environ["WOLFPAW_TEST_DATABASE_URL"])


# --- proposed → approved / rejected state machine -------------------------


async def test_store_proposed_then_approve_makes_tool_discoverable():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    embedder = StubEmbedder(dimensions=1024)
    vec = (await embedder.embed_one("emit a csv from rows")).vectors[0]

    conn = await _conn()
    try:
        tool_id = await tools_dao.store_proposed(
            conn, user_id=uid,
            name="emit_csv",
            description="Emit a CSV from a list of rows.",
            signature={"type": "object", "properties": {}},
            implementation="result = {'rows': len(inputs['rows'])}",
            embedding=vec,
            source_plan_id=None, source_task_id=None,
        )
        # Before approval, find_active_by_name returns None.
        pre = await tools_dao.find_active_by_name(
            conn, user_id=uid, name="emit_csv",
        )
        assert pre is None

        ok = await tools_dao.mark_approved(conn, tool_id=tool_id)
        assert ok is True

        post = await tools_dao.find_active_by_name(
            conn, user_id=uid, name="emit_csv",
        )
    finally:
        await conn.close()
    assert post is not None
    assert post.id == tool_id
    assert post.name == "emit_csv"
    assert post.status == "approved"
    assert post.approved_at is not None


async def test_mark_approved_is_idempotent():
    """Second mark_approved against an already-approved row returns
    False (the state machine has moved on)."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)

    conn = await _conn()
    try:
        tid = await tools_dao.store_proposed(
            conn, user_id=uid, name="t", description="t",
            signature={"type": "object", "properties": {}},
            implementation="result = {}",
            embedding=None, source_plan_id=None, source_task_id=None,
        )
        first = await tools_dao.mark_approved(conn, tool_id=tid)
        second = await tools_dao.mark_approved(conn, tool_id=tid)
    finally:
        await conn.close()
    assert first is True
    assert second is False


async def test_mark_rejected_keeps_row_undispatchable():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)

    conn = await _conn()
    try:
        tid = await tools_dao.store_proposed(
            conn, user_id=uid, name="rejected_tool", description="t",
            signature={"type": "object", "properties": {}},
            implementation="result = {}",
            embedding=None, source_plan_id=None, source_task_id=None,
        )
        ok = await tools_dao.mark_rejected(conn, tool_id=tid)
        assert ok is True

        found = await tools_dao.find_active_by_name(
            conn, user_id=uid, name="rejected_tool",
        )
    finally:
        await conn.close()
    assert found is None  # rejected rows never surface


async def test_approved_then_rejected_is_no_op():
    """The state machine only flips from `proposed`. Approved → reject
    attempts return False without changing the row."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)

    conn = await _conn()
    try:
        tid = await tools_dao.store_proposed(
            conn, user_id=uid, name="x", description="x",
            signature={"type": "object", "properties": {}},
            implementation="result = {}",
            embedding=None, source_plan_id=None, source_task_id=None,
        )
        await tools_dao.mark_approved(conn, tool_id=tid)
        flipped = await tools_dao.mark_rejected(conn, tool_id=tid)
        # find_active_by_name still surfaces the row (approval not
        # rolled back).
        active = await tools_dao.find_active_by_name(
            conn, user_id=uid, name="x",
        )
    finally:
        await conn.close()
    assert flipped is False
    assert active is not None
    assert active.status == "approved"


# --- user-scoping invariants ----------------------------------------------


async def test_find_active_by_name_is_user_scoped():
    """Alice's approved tool with name X must NOT surface when Bob
    looks up name X."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    alice = await _seed_user(dsn)
    bob = await _seed_user(dsn)

    conn = await _conn()
    try:
        tid = await tools_dao.store_proposed(
            conn, user_id=alice, name="alice_only",
            description="d",
            signature={"type": "object", "properties": {}},
            implementation="result = {}",
            embedding=None, source_plan_id=None, source_task_id=None,
        )
        await tools_dao.mark_approved(conn, tool_id=tid)

        alice_hit = await tools_dao.find_active_by_name(
            conn, user_id=alice, name="alice_only",
        )
        bob_hit = await tools_dao.find_active_by_name(
            conn, user_id=bob, name="alice_only",
        )
    finally:
        await conn.close()
    assert alice_hit is not None
    assert bob_hit is None


async def test_list_approved_for_user_excludes_proposed_and_rejected():
    """Only approved rows surface in list_approved_for_user."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)

    conn = await _conn()
    try:
        # One of each state.
        approved_id = await tools_dao.store_proposed(
            conn, user_id=uid, name="approved", description="d",
            signature={"type": "object", "properties": {}},
            implementation="result = {}",
            embedding=None, source_plan_id=None, source_task_id=None,
        )
        await tools_dao.mark_approved(conn, tool_id=approved_id)

        rejected_id = await tools_dao.store_proposed(
            conn, user_id=uid, name="rejected", description="d",
            signature={"type": "object", "properties": {}},
            implementation="result = {}",
            embedding=None, source_plan_id=None, source_task_id=None,
        )
        await tools_dao.mark_rejected(conn, tool_id=rejected_id)

        await tools_dao.store_proposed(
            conn, user_id=uid, name="proposed", description="d",
            signature={"type": "object", "properties": {}},
            implementation="result = {}",
            embedding=None, source_plan_id=None, source_task_id=None,
        )

        active = await tools_dao.list_approved_for_user(
            conn, user_id=uid,
        )
    finally:
        await conn.close()
    names = {t.name for t in active}
    assert names == {"approved"}


# --- cosine dedup search --------------------------------------------------


async def test_search_by_task_returns_closest_first():
    """Identical embeddings → similarity 1.0; the test plants the
    target vector and verifies that row comes first."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    embedder = StubEmbedder(dimensions=1024)
    target_text = "Emit a CSV from a list of rows."
    target_vec = (await embedder.embed_one(target_text)).vectors[0]
    other_vec = (await embedder.embed_one(
        "Fetch the Hacker News front page.")).vectors[0]

    conn = await _conn()
    try:
        target_id = await tools_dao.store_proposed(
            conn, user_id=uid, name="emit_csv",
            description=target_text,
            signature={"type": "object", "properties": {}},
            implementation="result = {}",
            embedding=target_vec, source_plan_id=None, source_task_id=None,
        )
        await tools_dao.mark_approved(conn, tool_id=target_id)

        other_id = await tools_dao.store_proposed(
            conn, user_id=uid, name="scrape_hn",
            description="hn frontpage",
            signature={"type": "object", "properties": {}},
            implementation="result = {}",
            embedding=other_vec, source_plan_id=None, source_task_id=None,
        )
        await tools_dao.mark_approved(conn, tool_id=other_id)

        hits = await tools_dao.search_by_task(
            conn, user_id=uid, query_embedding=target_vec, k=5,
        )
    finally:
        await conn.close()
    assert hits[0].id == target_id
    assert hits[0].similarity is not None and hits[0].similarity > 0.99


# --- schema invariants ----------------------------------------------------


async def test_unique_user_name_prevents_double_insert():
    """The UNIQUE(user_id, name) partial index rejects a second
    proposal under the same name for the same user."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)

    conn = await _conn()
    try:
        await tools_dao.store_proposed(
            conn, user_id=uid, name="dup", description="d",
            signature={"type": "object", "properties": {}},
            implementation="result = {}",
            embedding=None, source_plan_id=None, source_task_id=None,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await tools_dao.store_proposed(
                conn, user_id=uid, name="dup", description="d2",
                signature={"type": "object", "properties": {}},
                implementation="result = {}",
                embedding=None, source_plan_id=None, source_task_id=None,
            )
    finally:
        await conn.close()
