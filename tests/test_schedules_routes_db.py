"""HTTP tests for the schedules JSON API consumed by the React app's
Scheduled tab.

DB-gated so the routes hit real Postgres."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient

from wolfpaw.api import create_app
from wolfpaw.auth.deps import require_user_id
from wolfpaw.memory import schedules as schedules_dao
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
        for f in sorted(p.name for p in migrations_dir().glob("*.sql")):
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
            f"sched+{uuid4().hex[:6]}@test.local",
        )
    finally:
        await conn.close()


async def _conn():
    return await asyncpg.connect(dsn=os.environ["WOLFPAW_TEST_DATABASE_URL"])


def _client(uid: UUID) -> TestClient:
    app = create_app()
    app.dependency_overrides[require_user_id] = lambda: uid
    return TestClient(app)


def _soon(minutes: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def test_list_schedules_requires_auth():
    client = TestClient(create_app())
    assert client.get("/schedules").status_code == 401


async def test_list_schedules_newest_first_with_cadence():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        await schedules_dao.create(
            conn, user_id=uid, instruction="older", recurrence="once",
            next_run_at=_soon(60), title="older",
        )
        await schedules_dao.create(
            conn, user_id=uid, instruction="newer", recurrence="cron",
            next_run_at=_soon(5), cron_expr="0 8 * * *", title="newer",
        )
    finally:
        await conn.close()
    r = _client(uid).get("/schedules")
    assert r.status_code == 200
    rows = r.json()["schedules"]
    # Newest-created first, mirroring the Tasks tab.
    assert [s["title"] for s in rows] == ["newer", "older"]
    assert rows[0]["cadence"] == "cron: 0 8 * * * (UTC)"


async def test_list_schedules_humanizes_interval():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        await schedules_dao.create(
            conn, user_id=uid, instruction="poll", recurrence="interval",
            next_run_at=_soon(5), interval_seconds=3600, title="hourly",
        )
    finally:
        await conn.close()
    rows = _client(uid).get("/schedules").json()["schedules"]
    assert rows[0]["cadence"] == "every 1h"


async def test_list_schedules_includes_terminal_with_status():
    """Terminal rows (cancelled/done) stay visible with their real status,
    like the Tasks tab — not filtered out."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        s = await schedules_dao.create(
            conn, user_id=uid, instruction="cancelled-one", recurrence="once",
            next_run_at=_soon(5), title="cancelled-one",
        )
        await schedules_dao.cancel(conn, user_id=uid, schedule_id=s.id)
        await schedules_dao.create(
            conn, user_id=uid, instruction="active-one", recurrence="once",
            next_run_at=_soon(5), title="active-one",
        )
    finally:
        await conn.close()
    rows = _client(uid).get("/schedules").json()["schedules"]
    # Both present, newest first, each with its real status.
    by_title = {s["title"]: s["status"] for s in rows}
    assert by_title == {"active-one": "active", "cancelled-one": "cancelled"}
    assert [s["title"] for s in rows] == ["active-one", "cancelled-one"]


async def test_list_schedules_doesnt_leak_other_users():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    alice = await _seed_user(dsn)
    bob = await _seed_user(dsn)
    conn = await _conn()
    try:
        await schedules_dao.create(
            conn, user_id=alice, instruction="alice's", recurrence="once",
            next_run_at=_soon(5), title="secret",
        )
    finally:
        await conn.close()
    assert _client(bob).get("/schedules").json()["schedules"] == []
