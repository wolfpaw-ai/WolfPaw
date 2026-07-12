"""DB-backed tests for the `pending_questions` durable HITL store.

The load-bearing behavior: `wait_for_answer` blocks until another caller
(possibly another process) resolves the row, and wakes promptly via
Postgres LISTEN/NOTIFY — not just on the backstop poll. Also covers the
user-scoping on `mark_answered` and the open-question lookup Telegram uses.

Skipped without WOLFPAW_TEST_DATABASE_URL."""

from __future__ import annotations

import asyncio
import os
from uuid import UUID, uuid4

import asyncpg
import pytest

from wolfpaw.memory import pending_questions as pq_dao
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
        await apply_sql_file(conn, migrations_dir() / "001_init.sql")
        await apply_sql_file(conn, migrations_dir() / "019_pending_questions.sql")
    finally:
        await conn.close()
    await close_pool()
    yield
    await close_pool()


async def _seed_task(dsn: str) -> tuple[UUID, UUID]:
    """Insert a user + task, return (user_id, task_id)."""
    conn = await asyncpg.connect(dsn=dsn)
    try:
        user_id = await conn.fetchval(
            "INSERT INTO users (email) VALUES ($1) RETURNING id",
            f"u{uuid4().hex}@example.com",
        )
        task_id = await conn.fetchval(
            "INSERT INTO tasks (user_id, title) VALUES ($1, $2) RETURNING id",
            user_id, "test task",
        )
        return user_id, task_id
    finally:
        await conn.close()


async def _create_q(user_id, task_id, **kw):
    from wolfpaw.memory.db import acquire

    async with acquire() as conn:
        return await pq_dao.create(
            conn, task_id=task_id, user_id=user_id,
            question=kw.get("question", "proceed?"),
            options=kw.get("options"), channel=kw.get("channel", "web"),
        )


async def test_wait_wakes_on_notify_not_backstop():
    """wait_for_answer must return within a beat of mark_answered — proving
    the NOTIFY path fires, not the 15s backstop poll."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    user_id, task_id = await _seed_task(dsn)
    pq = await _create_q(user_id, task_id)

    from wolfpaw.memory.db import acquire

    async def answer_soon():
        await asyncio.sleep(0.2)
        async with acquire() as conn:
            return await pq_dao.mark_answered(
                conn, question_id=pq.id, user_id=user_id, answer="yes",
            )

    waiter = asyncio.create_task(
        pq_dao.wait_for_answer(question_id=pq.id, timeout=10.0))
    answerer = asyncio.create_task(answer_soon())

    resolved = await asyncio.wait_for(waiter, timeout=3.0)  # << backstop
    outcome = await answerer

    assert outcome is pq_dao.AnswerOutcome.ANSWERED
    assert resolved is not None
    assert resolved.status == "answered"
    assert resolved.answer == "yes"


async def test_wait_returns_none_on_timeout():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    user_id, task_id = await _seed_task(dsn)
    pq = await _create_q(user_id, task_id)
    resolved = await pq_dao.wait_for_answer(question_id=pq.id, timeout=0.5)
    assert resolved is None


async def test_mark_answered_is_user_scoped():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    user_id, task_id = await _seed_task(dsn)
    other_user, _ = await _seed_task(dsn)
    pq = await _create_q(user_id, task_id)

    from wolfpaw.memory.db import acquire

    async with acquire() as conn:
        # Wrong user can't answer.
        assert await pq_dao.mark_answered(
            conn, question_id=pq.id, user_id=other_user, answer="x",
        ) is pq_dao.AnswerOutcome.UNKNOWN
        # Right user can.
        assert await pq_dao.mark_answered(
            conn, question_id=pq.id, user_id=user_id, answer="ok",
        ) is pq_dao.AnswerOutcome.ANSWERED
        # Second answer is rejected as already-answered.
        assert await pq_dao.mark_answered(
            conn, question_id=pq.id, user_id=user_id, answer="again",
        ) is pq_dao.AnswerOutcome.ALREADY


async def test_get_open_for_user_finds_pending():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    user_id, task_id = await _seed_task(dsn)
    from wolfpaw.memory.db import acquire

    async with acquire() as conn:
        assert await pq_dao.get_open_for_user(conn, user_id=user_id) is None
    pq = await _create_q(user_id, task_id, question="which url?")
    async with acquire() as conn:
        found = await pq_dao.get_open_for_user(conn, user_id=user_id)
        assert found is not None and found.id == pq.id
        # Once answered, it's no longer "open".
        await pq_dao.mark_answered(
            conn, question_id=pq.id, user_id=user_id, answer="done")
        assert await pq_dao.get_open_for_user(conn, user_id=user_id) is None
