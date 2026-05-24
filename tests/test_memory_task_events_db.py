"""DB-backed tests for `memory.task_events` + migration 005's NOT-NULL
relaxation on `task_events.task_id`."""

from __future__ import annotations

import json
import os
from uuid import uuid4

import asyncpg
import pytest

from wolfpaw.memory import task_events
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


async def test_append_event_with_null_task_id_after_migration_005():
    """Migration 005 dropped NOT NULL on task_events.task_id so the
    Post-Evaluator can emit scoring events before tasks lifecycle
    (step 15) is online."""
    conn = await _conn()
    try:
        event_id = await task_events.append_event(
            conn,
            task_id=None,
            event_type="plan_scored",
            content={"plan_id": str(uuid4()), "score": 88, "summary": "good"},
        )
        row = await conn.fetchrow(
            "SELECT event_type, task_id, content"
            "  FROM task_events WHERE id = $1",
            event_id,
        )
    finally:
        await conn.close()
    assert row["event_type"] == "plan_scored"
    assert row["task_id"] is None
    content = json.loads(row["content"])
    assert content["score"] == 88


async def test_append_event_records_default_empty_content():
    conn = await _conn()
    try:
        event_id = await task_events.append_event(
            conn, task_id=None, event_type="noop",
        )
        row = await conn.fetchrow(
            "SELECT content FROM task_events WHERE id = $1", event_id,
        )
    finally:
        await conn.close()
    assert json.loads(row["content"]) == {}
