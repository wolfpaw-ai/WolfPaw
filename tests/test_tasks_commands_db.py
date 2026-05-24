"""DB-backed tests for `/tasks`, `/task <id>`, `/cancel <id>`."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import pytest

# Importing the module registers the commands.
import wolfpaw.tasks.commands  # noqa: F401
from wolfpaw.channels import InboundMessage
from wolfpaw.channels.commands import get_dispatcher
from wolfpaw.memory import tasks as tasks_dao
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
        ):
            await apply_sql_file(conn, migrations_dir() / f)
    finally:
        await conn.close()
    await close_pool()
    yield
    await close_pool()


async def _conn():
    return await asyncpg.connect(dsn=os.environ["WOLFPAW_TEST_DATABASE_URL"])


async def _seed_user(dsn: str) -> UUID:
    conn = await asyncpg.connect(dsn=dsn)
    try:
        return await conn.fetchval(
            "INSERT INTO users (email, email_verified) VALUES ($1, TRUE)"
            " RETURNING id",
            f"cmd+{uuid4().hex[:8]}@test.local",
        )
    finally:
        await conn.close()


def _msg(user_id: UUID, content: str) -> InboundMessage:
    return InboundMessage(
        user_id=user_id, content=content, channel_name="web",
    )


# --- tests ----------------------------------------------------------------


async def test_tasks_command_empty_state():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    result = await get_dispatcher().dispatch(_msg(uid, "/tasks"))
    assert result is not None
    assert "No tasks yet" in result.text


async def test_tasks_command_lists_existing():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        t1 = await tasks_dao.create(conn, user_id=uid, title="research X")
        t2 = await tasks_dao.create(conn, user_id=uid, title="draft Y")
    finally:
        await conn.close()
    result = await get_dispatcher().dispatch(_msg(uid, "/tasks"))
    assert result is not None
    assert "Recent tasks:" in result.text
    assert "research X" in result.text
    assert "draft Y" in result.text
    assert str(t1.id) in result.text


async def test_task_command_missing_id():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    result = await get_dispatcher().dispatch(_msg(uid, "/task"))
    assert result is not None
    assert "Usage" in result.text


async def test_task_command_unknown_id_returns_helpful_message():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    ghost = uuid4()
    result = await get_dispatcher().dispatch(_msg(uid, f"/task {ghost}"))
    assert result is not None
    assert "No task" in result.text


async def test_task_command_shows_status_and_events():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        t = await tasks_dao.create(conn, user_id=uid, title="my-job")
        await tasks_dao.mark_started(conn, task_id=t.id)
        await tasks_dao.mark_completed(conn, task_id=t.id)
        from wolfpaw.memory import task_events
        await task_events.append_event(
            conn, task_id=t.id, event_type="status.completed",
        )
    finally:
        await conn.close()
    result = await get_dispatcher().dispatch(_msg(uid, f"/task {t.id}"))
    assert result is not None
    assert "my-job" in result.text
    assert "completed" in result.text
    assert "status.completed" in result.text


async def test_task_command_doesnt_leak_other_users_task():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    alice = await _seed_user(dsn)
    bob = await _seed_user(dsn)
    conn = await _conn()
    try:
        t = await tasks_dao.create(conn, user_id=alice, title="alice's")
    finally:
        await conn.close()
    result = await get_dispatcher().dispatch(_msg(bob, f"/task {t.id}"))
    assert result is not None
    assert "No task" in result.text


async def test_cancel_command_marks_cancelled():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        t = await tasks_dao.create(conn, user_id=uid, title="cancel-me")
    finally:
        await conn.close()
    result = await get_dispatcher().dispatch(_msg(uid, f"/cancel {t.id}"))
    assert result is not None
    assert "Cancelled" in result.text
    conn = await _conn()
    try:
        row = await tasks_dao.get_by_id(conn, user_id=uid, task_id=t.id)
    finally:
        await conn.close()
    assert row is not None and row.status == "cancelled"


async def test_cancel_command_rejects_terminal_task():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        t = await tasks_dao.create(conn, user_id=uid, title="done")
        await tasks_dao.mark_completed(conn, task_id=t.id)
    finally:
        await conn.close()
    result = await get_dispatcher().dispatch(_msg(uid, f"/cancel {t.id}"))
    assert result is not None
    assert "couldn't be cancelled" in result.text or "already terminal" in result.text


async def test_cancel_command_invalid_uuid():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    result = await get_dispatcher().dispatch(_msg(uid, "/cancel not-a-uuid"))
    assert result is not None
    assert "Usage" in result.text
