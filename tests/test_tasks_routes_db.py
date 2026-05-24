"""HTTP tests for the tasks JSON API consumed by the React app.

DB-gated so the routes hit real Postgres."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient

from wolfpaw.api import create_app
from wolfpaw.auth.deps import require_user_id
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
            "006_telegram.sql",
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
            f"routes+{uuid4().hex[:6]}@test.local",
        )
    finally:
        await conn.close()


async def _conn():
    return await asyncpg.connect(dsn=os.environ["WOLFPAW_TEST_DATABASE_URL"])


def _client(uid: UUID) -> TestClient:
    app = create_app()
    app.dependency_overrides[require_user_id] = lambda: uid
    return TestClient(app)


def test_list_tasks_requires_auth():
    app = create_app()
    client = TestClient(app)
    r = client.get("/tasks")
    assert r.status_code == 401


async def test_list_tasks_returns_user_tasks_newest_first():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        a = await tasks_dao.create(conn, user_id=uid, title="A")
        b = await tasks_dao.create(conn, user_id=uid, title="B")
    finally:
        await conn.close()
    client = _client(uid)
    r = client.get("/tasks")
    assert r.status_code == 200
    body = r.json()
    titles = [t["title"] for t in body["tasks"]]
    assert titles == ["B", "A"]


async def test_list_tasks_doesnt_leak_other_users():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    alice = await _seed_user(dsn)
    bob = await _seed_user(dsn)
    conn = await _conn()
    try:
        await tasks_dao.create(conn, user_id=alice, title="alice's secret")
    finally:
        await conn.close()
    client = _client(bob)
    r = client.get("/tasks")
    assert r.status_code == 200
    assert r.json()["tasks"] == []


async def test_get_task_includes_events():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        t = await tasks_dao.create(conn, user_id=uid, title="my-task")
        from wolfpaw.memory import task_events
        await task_events.append_event(
            conn, task_id=t.id, event_type="status.pending",
        )
        await task_events.append_event(
            conn, task_id=t.id, event_type="status.running",
        )
    finally:
        await conn.close()
    client = _client(uid)
    r = client.get(f"/tasks/{t.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["task"]["title"] == "my-task"
    assert [e["event_type"] for e in body["events"]] == [
        "status.pending", "status.running",
    ]


async def test_get_task_404_for_other_users_task():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    alice = await _seed_user(dsn)
    bob = await _seed_user(dsn)
    conn = await _conn()
    try:
        t = await tasks_dao.create(conn, user_id=alice, title="alice's")
    finally:
        await conn.close()
    client = _client(bob)
    r = client.get(f"/tasks/{t.id}")
    assert r.status_code == 404


async def test_cancel_task_happy_path():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        t = await tasks_dao.create(conn, user_id=uid, title="cancel-me")
    finally:
        await conn.close()
    client = _client(uid)
    r = client.post(f"/tasks/{t.id}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"


async def test_cancel_task_409_when_already_terminal():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        t = await tasks_dao.create(conn, user_id=uid, title="done")
        await tasks_dao.mark_completed(conn, task_id=t.id)
    finally:
        await conn.close()
    client = _client(uid)
    r = client.post(f"/tasks/{t.id}/cancel")
    assert r.status_code == 409
    assert "completed" in r.json()["detail"]


async def test_cancel_task_404_when_not_owned():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    alice = await _seed_user(dsn)
    bob = await _seed_user(dsn)
    conn = await _conn()
    try:
        t = await tasks_dao.create(conn, user_id=alice, title="alice's")
    finally:
        await conn.close()
    client = _client(bob)
    r = client.post(f"/tasks/{t.id}/cancel")
    assert r.status_code == 404
