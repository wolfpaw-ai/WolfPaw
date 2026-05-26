"""Unit + DB-gated tests for the Sleep Cycle (step 26)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import asyncpg
import pytest

from wolfpaw.memory import skills as skills_mem
from wolfpaw.workers.jobs.sleep_cycle import (
    SleepCycleResult,
    _gc_orphan_threads,
    _pick_survivor,
    sleep_cycle,
    sleep_cycle_job,
)


# --- pure unit: survivor selection ----------------------------------------


def _skill(*, score: int | None = None, created_at=None) -> skills_mem.Skill:
    return skills_mem.Skill(
        id=uuid4(), user_id=uuid4(),
        name=f"s_{uuid4().hex[:6]}",
        description="desc",
        score=score, created_at=created_at,
    )


def test_pick_survivor_prefers_higher_score():
    low = _skill(score=70)
    high = _skill(score=95)
    loser, survivor = _pick_survivor(low, high)
    assert loser is low and survivor is high


def test_pick_survivor_prefers_higher_score_when_first_arg_wins():
    high = _skill(score=95)
    low = _skill(score=70)
    loser, survivor = _pick_survivor(high, low)
    assert loser is low and survivor is high


def test_pick_survivor_on_score_tie_keeps_newer():
    now = datetime.now(timezone.utc)
    old = _skill(score=85, created_at=now - timedelta(days=10))
    new = _skill(score=85, created_at=now)
    loser, survivor = _pick_survivor(old, new)
    assert loser is old and survivor is new
    # And the reverse arg order:
    loser, survivor = _pick_survivor(new, old)
    assert loser is old and survivor is new


def test_pick_survivor_handles_none_scores():
    """A skill with score=None scores worst — newly-emitted skills
    that haven't been re-scored yet shouldn't beat scored ones."""
    scored = _skill(score=50)
    unscored = _skill(score=None)
    loser, survivor = _pick_survivor(unscored, scored)
    assert loser is unscored and survivor is scored


# --- orchestrator gating --------------------------------------------------


async def test_sleep_cycle_no_ops_when_disabled(monkeypatch):
    """Default (disabled) → orchestrator returns zero counters without
    touching any of the three operations."""
    from wolfpaw.config import get_settings

    monkeypatch.setenv("WOLFPAW_SLEEP_CYCLE_ENABLED", "false")
    get_settings.cache_clear()  # type: ignore[attr-defined]

    called: list[str] = []

    async def fake_rescore(*, batch):
        called.append("rescore")
        return 0, 0

    async def fake_consolidate(*, threshold):
        called.append("consolidate")
        return 0

    async def fake_gc(*, age_days):
        called.append("gc")
        return 0

    monkeypatch.setattr(
        "wolfpaw.workers.jobs.sleep_cycle._rescore_old_plans", fake_rescore,
    )
    monkeypatch.setattr(
        "wolfpaw.workers.jobs.sleep_cycle._consolidate_skills", fake_consolidate,
    )
    monkeypatch.setattr(
        "wolfpaw.workers.jobs.sleep_cycle._gc_orphan_threads", fake_gc,
    )

    result = await sleep_cycle()
    assert result == SleepCycleResult(0, 0, 0, 0)
    assert called == [], "no operations should run when disabled"


async def test_sleep_cycle_aggregates_counts_when_enabled(monkeypatch):
    from wolfpaw.config import get_settings

    monkeypatch.setenv("WOLFPAW_SLEEP_CYCLE_ENABLED", "true")
    get_settings.cache_clear()  # type: ignore[attr-defined]

    async def fake_rescore(*, batch):
        return 5, 1

    async def fake_consolidate(*, threshold):
        return 2

    async def fake_gc(*, age_days):
        return 7

    monkeypatch.setattr(
        "wolfpaw.workers.jobs.sleep_cycle._rescore_old_plans", fake_rescore,
    )
    monkeypatch.setattr(
        "wolfpaw.workers.jobs.sleep_cycle._consolidate_skills", fake_consolidate,
    )
    monkeypatch.setattr(
        "wolfpaw.workers.jobs.sleep_cycle._gc_orphan_threads", fake_gc,
    )

    result = await sleep_cycle()
    assert result == SleepCycleResult(
        rescored_plans=5, rescore_errors=1,
        skills_superseded=2, orphan_threads_deleted=7,
    )


async def test_sleep_cycle_job_returns_counter_dict(monkeypatch):
    """The arq wrapper returns a dict so the cron history can show the
    last run's stats — useful for ops without rummaging through logs."""
    from wolfpaw.config import get_settings

    monkeypatch.setenv("WOLFPAW_SLEEP_CYCLE_ENABLED", "true")
    get_settings.cache_clear()  # type: ignore[attr-defined]

    async def fake_rescore(*, batch):
        return 3, 0

    async def fake_consolidate(*, threshold):
        return 1

    async def fake_gc(*, age_days):
        return 0

    monkeypatch.setattr(
        "wolfpaw.workers.jobs.sleep_cycle._rescore_old_plans", fake_rescore,
    )
    monkeypatch.setattr(
        "wolfpaw.workers.jobs.sleep_cycle._consolidate_skills", fake_consolidate,
    )
    monkeypatch.setattr(
        "wolfpaw.workers.jobs.sleep_cycle._gc_orphan_threads", fake_gc,
    )

    result = await sleep_cycle_job({})
    assert result == {
        "rescored_plans": 3, "rescore_errors": 0,
        "skills_superseded": 1, "orphan_threads_deleted": 0,
    }


# --- DB-gated: operations end-to-end --------------------------------------


_db_required = pytest.mark.skipif(
    not os.getenv("WOLFPAW_TEST_DATABASE_URL"),
    reason="WOLFPAW_TEST_DATABASE_URL not set",
)


@pytest.fixture
async def fresh_db(monkeypatch):
    """Reset schema + apply all migrations. Local to this module so it
    can't leak into other tests."""
    if not os.getenv("WOLFPAW_TEST_DATABASE_URL"):
        yield None
        return

    from wolfpaw.config import get_settings
    from wolfpaw.memory.db import apply_sql_file, close_pool, migrations_dir

    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    monkeypatch.setenv("WOLFPAW_DATABASE_URL", dsn)
    get_settings.cache_clear()  # type: ignore[attr-defined]

    conn = await asyncpg.connect(dsn=dsn)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        for f in (
            "001_init.sql", "002_auth.sql", "003_sandbox.sql",
            "004_seed_skills.sql", "005_post_evaluator.sql",
            "008_conv_compaction.sql", "009_skill_distiller.sql",
            "010_skill_supersession.sql",
        ):
            await apply_sql_file(conn, migrations_dir() / f)
    finally:
        await conn.close()
    await close_pool()
    yield dsn
    await close_pool()


async def _seed_user(dsn: str) -> UUID:
    conn = await asyncpg.connect(dsn=dsn)
    try:
        return await conn.fetchval(
            "INSERT INTO users (email, email_verified) VALUES ($1, TRUE)"
            " RETURNING id",
            f"sleep+{uuid4().hex[:8]}@test.local",
        )
    finally:
        await conn.close()


@_db_required
async def test_gc_orphan_threads_deletes_old_empty_threads(fresh_db):
    """Threads with no messages and created_at past the cutoff get
    deleted. Threads with messages or recent threads are kept."""
    dsn = fresh_db
    uid = await _seed_user(dsn)
    conn = await asyncpg.connect(dsn=dsn)
    try:
        # Old + empty → should be deleted.
        old_empty = await conn.fetchval(
            "INSERT INTO threads (user_id, channel, created_at)"
            " VALUES ($1, 'web', NOW() - INTERVAL '60 days') RETURNING id",
            uid,
        )
        # Recent + empty → keep.
        recent_empty = await conn.fetchval(
            "INSERT INTO threads (user_id, channel, created_at)"
            " VALUES ($1, 'web', NOW() - INTERVAL '5 days') RETURNING id",
            uid,
        )
        # Old + has a message → keep.
        old_with_msg = await conn.fetchval(
            "INSERT INTO threads (user_id, channel, created_at)"
            " VALUES ($1, 'web', NOW() - INTERVAL '60 days') RETURNING id",
            uid,
        )
        await conn.execute(
            "INSERT INTO messages (thread_id, role, content)"
            " VALUES ($1, 'user', 'hi')",
            old_with_msg,
        )
    finally:
        await conn.close()

    deleted = await _gc_orphan_threads(age_days=30)
    assert deleted == 1

    # Verify the right row is gone.
    conn = await asyncpg.connect(dsn=dsn)
    try:
        existing = {
            r["id"]
            for r in await conn.fetch("SELECT id FROM threads")
        }
    finally:
        await conn.close()
    assert old_empty not in existing
    assert recent_empty in existing
    assert old_with_msg in existing


@_db_required
async def test_consolidate_skills_supersedes_lower_scoring_near_duplicate(
    fresh_db, monkeypatch,
):
    """Two emitted skills with very similar embeddings get merged: the
    lower-scored one ends up with ``superseded_by_skill_id`` pointing
    at the higher-scored one."""
    from wolfpaw.embeddings.stub import StubEmbedder
    from wolfpaw.memory import procedural
    from wolfpaw.workers.jobs.sleep_cycle import _consolidate_skills

    dsn = fresh_db
    uid = await _seed_user(dsn)
    embedder = StubEmbedder(dimensions=1024)

    # Use the same source text so both skills get identical embeddings —
    # cosine similarity will be 1.0 against any threshold.
    same_text = "Research N vendors and emit a comparison table."
    vec = (await embedder.embed_one(same_text)).vectors[0]

    conn = await asyncpg.connect(dsn=dsn)
    try:
        plan_a = await procedural.store(
            conn, user_id=uid, thread_id=None, task_id=None,
            query="q", query_embedding=vec,
            steps=[{"id": "a", "kind": "reasoning", "description": "t"}],
        )
        plan_b = await procedural.store(
            conn, user_id=uid, thread_id=None, task_id=None,
            query="q", query_embedding=vec,
            steps=[{"id": "a", "kind": "reasoning", "description": "t"}],
        )
        low_score_id = await skills_mem.store_emitted(
            conn, user_id=uid,
            name="low_score_skill", description=same_text,
            embedding=vec, ingredients={}, steps=[
                {"id": "a", "kind": "reasoning", "description": "t"},
            ],
            source_plan_id=plan_a, score=80,
        )
        high_score_id = await skills_mem.store_emitted(
            conn, user_id=uid,
            name="high_score_skill", description=same_text,
            embedding=vec, ingredients={}, steps=[
                {"id": "a", "kind": "reasoning", "description": "t"},
            ],
            source_plan_id=plan_b, score=95,
        )
    finally:
        await conn.close()

    superseded = await _consolidate_skills(threshold=0.9)
    assert superseded == 1

    conn = await asyncpg.connect(dsn=dsn)
    try:
        low = await conn.fetchrow(
            "SELECT superseded_by_skill_id, superseded_at"
            "  FROM skills WHERE id = $1",
            low_score_id,
        )
        high = await conn.fetchrow(
            "SELECT superseded_by_skill_id"
            "  FROM skills WHERE id = $1",
            high_score_id,
        )
    finally:
        await conn.close()
    assert low["superseded_by_skill_id"] == high_score_id
    assert low["superseded_at"] is not None
    assert high["superseded_by_skill_id"] is None


@_db_required
async def test_consolidate_skips_when_similarity_below_threshold(fresh_db):
    """Two skills with distinct embeddings stay active — nothing is
    superseded."""
    from wolfpaw.embeddings.stub import StubEmbedder
    from wolfpaw.memory import procedural
    from wolfpaw.workers.jobs.sleep_cycle import _consolidate_skills

    dsn = fresh_db
    uid = await _seed_user(dsn)
    embedder = StubEmbedder(dimensions=1024)
    vec_a = (await embedder.embed_one("research vendors")).vectors[0]
    vec_b = (await embedder.embed_one(
        "summarize this earnings call transcript")).vectors[0]

    conn = await asyncpg.connect(dsn=dsn)
    try:
        plan_id = await procedural.store(
            conn, user_id=uid, thread_id=None, task_id=None,
            query="q", query_embedding=vec_a,
            steps=[{"id": "a", "kind": "reasoning", "description": "t"}],
        )
        await skills_mem.store_emitted(
            conn, user_id=uid, name="vendor_research",
            description="research vendors", embedding=vec_a,
            ingredients={}, steps=[
                {"id": "a", "kind": "reasoning", "description": "t"},
            ],
            source_plan_id=plan_id, score=90,
        )
        await skills_mem.store_emitted(
            conn, user_id=uid, name="earnings_summary",
            description="summarize earnings call transcript",
            embedding=vec_b, ingredients={}, steps=[
                {"id": "a", "kind": "reasoning", "description": "t"},
            ],
            source_plan_id=plan_id, score=90,
        )
    finally:
        await conn.close()

    # Threshold 0.92 is too strict for unrelated stub-derived texts.
    superseded = await _consolidate_skills(threshold=0.92)
    assert superseded == 0


@_db_required
async def test_search_by_task_filters_superseded(fresh_db):
    """A superseded skill must NOT surface from `search_by_task`."""
    from wolfpaw.embeddings.stub import StubEmbedder
    from wolfpaw.memory import procedural

    dsn = fresh_db
    uid = await _seed_user(dsn)
    embedder = StubEmbedder(dimensions=1024)
    vec = (await embedder.embed_one("compare vendors")).vectors[0]

    conn = await asyncpg.connect(dsn=dsn)
    try:
        plan_id = await procedural.store(
            conn, user_id=uid, thread_id=None, task_id=None,
            query="q", query_embedding=vec,
            steps=[{"id": "a", "kind": "reasoning", "description": "t"}],
        )
        loser = await skills_mem.store_emitted(
            conn, user_id=uid, name="loser",
            description="compare vendors", embedding=vec,
            ingredients={}, steps=[
                {"id": "a", "kind": "reasoning", "description": "t"},
            ],
            source_plan_id=plan_id, score=70,
        )
        survivor = await skills_mem.store_emitted(
            conn, user_id=uid, name="survivor",
            description="compare vendors", embedding=vec,
            ingredients={}, steps=[
                {"id": "a", "kind": "reasoning", "description": "t"},
            ],
            source_plan_id=plan_id, score=95,
        )
        await skills_mem.mark_superseded(
            conn, skill_id=loser, superseded_by=survivor,
        )
        hits = await skills_mem.search_by_task(
            conn, user_id=uid, query_embedding=vec, k=10,
        )
    finally:
        await conn.close()
    names = {s.name for s in hits}
    assert "survivor" in names
    assert "loser" not in names


@_db_required
async def test_mark_superseded_is_idempotent(fresh_db):
    """A second mark_superseded call against an already-superseded
    skill returns False rather than re-stamping ``superseded_at``."""
    from wolfpaw.embeddings.stub import StubEmbedder
    from wolfpaw.memory import procedural

    dsn = fresh_db
    uid = await _seed_user(dsn)
    embedder = StubEmbedder(dimensions=1024)
    vec = (await embedder.embed_one("x")).vectors[0]

    conn = await asyncpg.connect(dsn=dsn)
    try:
        plan_id = await procedural.store(
            conn, user_id=uid, thread_id=None, task_id=None,
            query="q", query_embedding=vec,
            steps=[{"id": "a", "kind": "reasoning", "description": "t"}],
        )
        a = await skills_mem.store_emitted(
            conn, user_id=uid, name="a", description="x", embedding=vec,
            ingredients={}, steps=[
                {"id": "s", "kind": "reasoning", "description": "t"},
            ],
            source_plan_id=plan_id, score=70,
        )
        b = await skills_mem.store_emitted(
            conn, user_id=uid, name="b", description="x", embedding=vec,
            ingredients={}, steps=[
                {"id": "s", "kind": "reasoning", "description": "t"},
            ],
            source_plan_id=plan_id, score=95,
        )
        first = await skills_mem.mark_superseded(
            conn, skill_id=a, superseded_by=b,
        )
        second = await skills_mem.mark_superseded(
            conn, skill_id=a, superseded_by=b,
        )
    finally:
        await conn.close()
    assert first is True
    assert second is False


@_db_required
async def test_rescore_old_plans_updates_score_via_evaluator(
    fresh_db, monkeypatch,
):
    """The re-score op pulls the oldest scored plan, hands it to the
    Post-Evaluator (mocked here), and writes back the new score."""
    from wolfpaw.embeddings.stub import StubEmbedder
    from wolfpaw.memory import procedural
    from wolfpaw.schemas import PostEvalVerdict
    from wolfpaw.workers.jobs.sleep_cycle import _rescore_old_plans

    dsn = fresh_db
    uid = await _seed_user(dsn)
    embedder = StubEmbedder(dimensions=1024)
    vec = (await embedder.embed_one("compare vendors")).vectors[0]

    # Seed two scored plans with final_answer present.
    conn = await asyncpg.connect(dsn=dsn)
    try:
        plan_a = await procedural.store(
            conn, user_id=uid, thread_id=None, task_id=None,
            query="compare vendors", query_embedding=vec,
            steps=[{"id": "s1", "kind": "reasoning", "description": "t"}],
            final_answer="the answer", success=True, score=80,
        )
        plan_b = await procedural.store(
            conn, user_id=uid, thread_id=None, task_id=None,
            query="another", query_embedding=vec,
            steps=[{"id": "s2", "kind": "reasoning", "description": "t"}],
            final_answer="another answer", success=True, score=90,
        )
    finally:
        await conn.close()

    # Stub the evaluator to return a fresh score deterministically.
    class _FakeEvaluator:
        def __init__(self):
            self.calls: list = []

        async def evaluate(self, *, ctx, plan, execution):
            self.calls.append({"plan_id": plan.id})
            return PostEvalVerdict(
                score=55, summary="re-scored",
                what_went_well="", what_went_wrong="", improvements="",
            )

    fake = _FakeEvaluator()
    monkeypatch.setattr(
        "wolfpaw.agents.post_evaluator.get_post_evaluator_agent",
        lambda: fake,
    )

    updated, errors = await _rescore_old_plans(batch=10)
    assert updated == 2
    assert errors == 0

    # The score in `plans` got overwritten.
    conn = await asyncpg.connect(dsn=dsn)
    try:
        scores = await conn.fetch(
            "SELECT id, score FROM plans WHERE id = ANY($1::uuid[])"
            " ORDER BY created_at ASC",
            [plan_a, plan_b],
        )
    finally:
        await conn.close()
    assert [r["score"] for r in scores] == [55, 55]


@_db_required
async def test_rescore_old_plans_skips_plans_without_final_answer(
    fresh_db, monkeypatch,
):
    """A plan whose Executor never persisted a final_answer can't be
    re-scored — the op skips it without erroring."""
    from wolfpaw.embeddings.stub import StubEmbedder
    from wolfpaw.memory import procedural
    from wolfpaw.workers.jobs.sleep_cycle import _rescore_old_plans

    dsn = fresh_db
    uid = await _seed_user(dsn)
    embedder = StubEmbedder(dimensions=1024)
    vec = (await embedder.embed_one("q")).vectors[0]

    conn = await asyncpg.connect(dsn=dsn)
    try:
        await procedural.store(
            conn, user_id=uid, thread_id=None, task_id=None,
            query="q", query_embedding=vec,
            steps=[{"id": "s", "kind": "reasoning", "description": "t"}],
            final_answer=None, success=None, score=70,
        )
    finally:
        await conn.close()

    class _FakeEvaluator:
        async def evaluate(self, **_kw):  # pragma: no cover - shouldn't fire
            raise AssertionError("should not be called")

    monkeypatch.setattr(
        "wolfpaw.agents.post_evaluator.get_post_evaluator_agent",
        lambda: _FakeEvaluator(),
    )

    updated, errors = await _rescore_old_plans(batch=10)
    assert (updated, errors) == (0, 0)
