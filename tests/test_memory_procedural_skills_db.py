"""DB-backed tests for procedural + skills memory.

Covers the pgvector retrieval queries end-to-end against a real
Postgres. Skipped without WOLFPAW_TEST_DATABASE_URL."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import pytest

from wolfpaw.embeddings.stub import StubEmbedder
from wolfpaw.memory import procedural, skills as skills_mem
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
            "001_init.sql", "002_auth.sql", "003_sandbox.sql", "004_seed_skills.sql",
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
            f"plan+{uuid4().hex[:8]}@test.local",
        )
    finally:
        await conn.close()


async def _conn():
    return await asyncpg.connect(dsn=os.environ["WOLFPAW_TEST_DATABASE_URL"])


# --- procedural ------------------------------------------------------------


async def test_procedural_store_then_search_finds_self():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    embedder = StubEmbedder(dimensions=1024)
    vec = (await embedder.embed_one("research vendor X")).vectors[0]

    conn = await _conn()
    try:
        plan_id = await procedural.store(
            conn, user_id=uid, thread_id=None, task_id=None,
            query="research vendor X", query_embedding=vec,
            steps=[{"id": "a", "kind": "reasoning", "description": "think"}],
        )
        results = await procedural.search_similar(
            conn, user_id=uid, query_embedding=vec, k=5,
        )
    finally:
        await conn.close()

    assert any(p.id == plan_id for p in results)
    top = results[0]
    assert top.id == plan_id
    assert top.similarity is not None and top.similarity > 0.99


async def test_procedural_search_isolated_per_user():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    alice = await _seed_user(dsn)
    bob = await _seed_user(dsn)
    embedder = StubEmbedder(dimensions=1024)
    vec = (await embedder.embed_one("alice's query")).vectors[0]

    conn = await _conn()
    try:
        await procedural.store(
            conn, user_id=alice, thread_id=None, task_id=None,
            query="alice's query", query_embedding=vec,
            steps=[],
        )
        bob_results = await procedural.search_similar(
            conn, user_id=bob, query_embedding=vec, k=5,
        )
    finally:
        await conn.close()
    assert bob_results == []


async def test_procedural_search_skips_rows_without_embedding():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    embedder = StubEmbedder(dimensions=1024)
    vec = (await embedder.embed_one("query")).vectors[0]

    conn = await _conn()
    try:
        # Insert a row with NULL embedding directly.
        await conn.execute(
            "INSERT INTO plans (user_id, query, query_embedding, steps)"
            " VALUES ($1, 'no-embedding', NULL, '[]'::jsonb)",
            uid,
        )
        results = await procedural.search_similar(
            conn, user_id=uid, query_embedding=vec, k=5,
        )
    finally:
        await conn.close()
    assert all(p.query != "no-embedding" for p in results)


async def test_procedural_min_score_filter():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    embedder = StubEmbedder(dimensions=1024)
    vec = (await embedder.embed_one("query")).vectors[0]

    conn = await _conn()
    try:
        low_id = await procedural.store(
            conn, user_id=uid, thread_id=None, task_id=None,
            query="low scored", query_embedding=vec, steps=[],
            success=False, score=30,
        )
        high_id = await procedural.store(
            conn, user_id=uid, thread_id=None, task_id=None,
            query="high scored", query_embedding=vec, steps=[],
            success=True, score=85,
        )
        filtered = await procedural.search_similar(
            conn, user_id=uid, query_embedding=vec, k=5, min_score=60,
        )
    finally:
        await conn.close()
    ids = {p.id for p in filtered}
    assert high_id in ids
    assert low_id not in ids


async def test_procedural_update_outcome():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    embedder = StubEmbedder(dimensions=1024)
    vec = (await embedder.embed_one("query")).vectors[0]

    conn = await _conn()
    try:
        plan_id = await procedural.store(
            conn, user_id=uid, thread_id=None, task_id=None,
            query="q", query_embedding=vec, steps=[],
        )
        await procedural.update_outcome(
            conn, plan_id=plan_id,
            final_answer="42", success=True, score=88,
        )
        row = await conn.fetchrow(
            "SELECT final_answer, success, score FROM plans WHERE id = $1",
            plan_id,
        )
    finally:
        await conn.close()
    assert row["final_answer"] == "42"
    assert row["success"] is True
    assert row["score"] == 88


async def test_procedural_update_outcome_partial_writes_dont_clobber():
    """Executor (step 13) writes final_answer + success without a score;
    Post-Evaluator (step 14) follows up with score. Neither should
    overwrite the other's untouched columns."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    embedder = StubEmbedder(dimensions=1024)
    vec = (await embedder.embed_one("query")).vectors[0]

    conn = await _conn()
    try:
        plan_id = await procedural.store(
            conn, user_id=uid, thread_id=None, task_id=None,
            query="q", query_embedding=vec, steps=[],
        )
        # Executor's write (no score).
        await procedural.update_outcome(
            conn, plan_id=plan_id,
            final_answer="answer", success=True,
        )
        # Post-Evaluator's later write (score only).
        await procedural.update_outcome(conn, plan_id=plan_id, score=77)
        row = await conn.fetchrow(
            "SELECT final_answer, success, score FROM plans WHERE id = $1",
            plan_id,
        )
    finally:
        await conn.close()
    assert row["final_answer"] == "answer"
    assert row["success"] is True
    assert row["score"] == 77


# --- skills ----------------------------------------------------------------


async def test_seed_starter_skills_is_idempotent():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    embedder = StubEmbedder(dimensions=1024)
    conn = await _conn()
    try:
        first = await skills_mem.seed_starter_skills(conn, embedder)
        second = await skills_mem.seed_starter_skills(conn, embedder)
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM skills WHERE user_id IS NULL"
        )
    finally:
        await conn.close()
    assert first == len(skills_mem.STARTER_SKILLS)
    assert second == 0
    assert count == len(skills_mem.STARTER_SKILLS)


async def test_skills_search_finds_seeded_skills_for_any_user():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    embedder = StubEmbedder(dimensions=1024)
    conn = await _conn()
    try:
        await skills_mem.seed_starter_skills(conn, embedder)
        vec = (await embedder.embed_one(
            "Research five vendors and put them in a spreadsheet"
        )).vectors[0]
        results = await skills_mem.search_by_task(
            conn, user_id=uid, query_embedding=vec, k=5,
        )
    finally:
        await conn.close()
    assert len(results) > 0
    # All hits are seeded (user_id IS NULL) since this user has no skills.
    assert all(s.user_id is None for s in results)


async def test_skills_search_orders_by_similarity():
    """A query embedded from the exact description should rank that skill first."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    embedder = StubEmbedder(dimensions=1024)
    conn = await _conn()
    try:
        await skills_mem.seed_starter_skills(conn, embedder)
        target = skills_mem.STARTER_SKILLS[0]
        vec = (await embedder.embed_one(target["description"])).vectors[0]
        results = await skills_mem.search_by_task(
            conn, user_id=uid, query_embedding=vec, k=5,
        )
    finally:
        await conn.close()
    assert results[0].name == target["name"]
    assert results[0].similarity is not None and results[0].similarity > 0.99


async def test_skills_search_skips_rows_without_embedding():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    embedder = StubEmbedder(dimensions=1024)
    conn = await _conn()
    try:
        await conn.execute(
            "INSERT INTO skills (user_id, name, description, embedding,"
            " ingredients, steps)"
            " VALUES (NULL, 'no_embed', 'd', NULL,"
            " '{}'::jsonb, '[]'::jsonb)"
        )
        vec = (await embedder.embed_one("query")).vectors[0]
        results = await skills_mem.search_by_task(
            conn, user_id=uid, query_embedding=vec, k=10,
        )
    finally:
        await conn.close()
    assert all(s.name != "no_embed" for s in results)


# --- step 25: store_emitted ----------------------------------------------


async def test_store_emitted_inserts_user_scoped_skill_pointing_at_source_plan():
    """The user-scoped emit path persists name + description + embedding
    + ingredients + steps + source_plan_id + score and returns the
    new id. Subsequent search_by_task surfaces it for the same user."""
    from wolfpaw.memory import procedural

    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    embedder = StubEmbedder(dimensions=1024)
    desc = "Research N vendors and emit a comparison table."
    desc_vec = (await embedder.embed_one(desc)).vectors[0]
    query_vec = (await embedder.embed_one("compare vendors")).vectors[0]

    conn = await _conn()
    try:
        plan_id = await procedural.store(
            conn, user_id=uid, thread_id=None, task_id=None,
            query="compare vendors", query_embedding=query_vec,
            steps=[{"id": "s1", "kind": "functional", "tool": "web_search",
                    "description": "find them"}],
            score=95, success=True,
        )

        new_id = await skills_mem.store_emitted(
            conn,
            user_id=uid,
            name="vendor_lookup_table_v2",
            description=desc, embedding=desc_vec,
            ingredients={"tools": ["web_search", "create_spreadsheet"]},
            steps=[
                {"id": "search", "kind": "functional", "tool": "web_search",
                 "description": "find homepages"},
            ],
            source_plan_id=plan_id, score=95,
        )

        row = await conn.fetchrow(
            "SELECT user_id, name, description, ingredients, steps,"
            "       source_plan_id, score"
            "  FROM skills WHERE id = $1",
            new_id,
        )
        # And the emitted skill is retrievable for the same user.
        hits = await skills_mem.search_by_task(
            conn, user_id=uid, query_embedding=desc_vec, k=5,
        )
    finally:
        await conn.close()

    assert row["user_id"] == uid
    assert row["name"] == "vendor_lookup_table_v2"
    assert row["description"] == desc
    assert row["source_plan_id"] == plan_id
    assert row["score"] == 95
    assert any(s.name == "vendor_lookup_table_v2" for s in hits)


async def test_store_emitted_does_not_surface_for_other_users():
    """A user's emitted skill stays scoped to their user_id — seeded
    rows (user_id IS NULL) are shared, but emitted rows are not."""
    from wolfpaw.memory import procedural

    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    alice = await _seed_user(dsn)
    bob = await _seed_user(dsn)
    embedder = StubEmbedder(dimensions=1024)
    desc = "Alice's private vendor table skill."
    desc_vec = (await embedder.embed_one(desc)).vectors[0]

    conn = await _conn()
    try:
        plan_id = await procedural.store(
            conn, user_id=alice, thread_id=None, task_id=None,
            query="alice-only", query_embedding=desc_vec,
            steps=[{"id": "x", "kind": "reasoning", "description": "t"}],
        )
        await skills_mem.store_emitted(
            conn,
            user_id=alice,
            name="alice_only_skill",
            description=desc, embedding=desc_vec,
            ingredients={}, steps=[
                {"id": "a", "kind": "reasoning", "description": "t"},
            ],
            source_plan_id=plan_id, score=95,
        )

        alice_hits = await skills_mem.search_by_task(
            conn, user_id=alice, query_embedding=desc_vec, k=5,
        )
        bob_hits = await skills_mem.search_by_task(
            conn, user_id=bob, query_embedding=desc_vec, k=5,
        )
    finally:
        await conn.close()

    assert any(s.name == "alice_only_skill" for s in alice_hits)
    assert all(s.name != "alice_only_skill" for s in bob_hits)
