"""DB-gated tests for `memory.tasks` + `memory.task_events.fetch_for_task`.

Exercises the full state machine + cross-user isolation."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import pytest

from wolfpaw.memory import task_events, tasks as tasks_dao
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
            f"task+{uuid4().hex[:8]}@test.local",
        )
    finally:
        await conn.close()


# --- create + read --------------------------------------------------------


async def test_create_starts_pending():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        t = await tasks_dao.create(
            conn, user_id=uid, title="research vendors", description="x",
            channel_for_completion="web",
        )
    finally:
        await conn.close()
    assert t.status == "pending"
    assert t.user_id == uid
    assert t.title == "research vendors"
    assert t.channel_for_completion == "web"


async def test_get_by_id_scoped_to_user():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    alice = await _seed_user(dsn)
    bob = await _seed_user(dsn)
    conn = await _conn()
    try:
        t = await tasks_dao.create(conn, user_id=alice, title="alice's")
        # Alice can read her own task.
        seen = await tasks_dao.get_by_id(conn, user_id=alice, task_id=t.id)
        # Bob cannot.
        not_seen = await tasks_dao.get_by_id(conn, user_id=bob, task_id=t.id)
    finally:
        await conn.close()
    assert seen is not None and seen.id == t.id
    assert not_seen is None


async def test_list_for_user_orders_newest_first_with_status_filter():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        a = await tasks_dao.create(conn, user_id=uid, title="A")
        b = await tasks_dao.create(conn, user_id=uid, title="B")
        c = await tasks_dao.create(conn, user_id=uid, title="C")
        await tasks_dao.mark_started(conn, task_id=b.id)
        await tasks_dao.mark_completed(conn, task_id=c.id)
        all_tasks = await tasks_dao.list_for_user(conn, user_id=uid)
        running = await tasks_dao.list_for_user(
            conn, user_id=uid, statuses=("running",),
        )
        terminal = await tasks_dao.list_for_user(
            conn, user_id=uid, statuses=("completed", "failed", "cancelled"),
        )
    finally:
        await conn.close()
    # Newest first.
    assert [t.title for t in all_tasks] == ["C", "B", "A"]
    assert [t.id for t in running] == [b.id]
    assert [t.id for t in terminal] == [c.id]


# --- state transitions ----------------------------------------------------


async def test_state_machine_happy_path():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        t = await tasks_dao.create(conn, user_id=uid, title="x")
        assert t.status == "pending"
        started = await tasks_dao.mark_started(conn, task_id=t.id)
        assert started is not None and started.status == "running"
        assert started.started_at is not None
        awaiting = await tasks_dao.mark_awaiting_user(
            conn, task_id=t.id, reason="ask_user: overwrite?",
        )
        assert awaiting is not None and awaiting.status == "awaiting_user"
        assert awaiting.blocking_reason == "ask_user: overwrite?"
        resumed = await tasks_dao.mark_started(conn, task_id=t.id)
        assert resumed is not None and resumed.blocking_reason is None
        completed = await tasks_dao.mark_completed(conn, task_id=t.id)
        assert completed is not None and completed.status == "completed"
        assert completed.completed_at is not None
    finally:
        await conn.close()


async def test_terminal_transitions_are_sticky():
    """Once completed/failed/cancelled, further transitions are no-ops."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        t = await tasks_dao.create(conn, user_id=uid, title="x")
        await tasks_dao.mark_completed(conn, task_id=t.id)
        # Try to restart — should be a no-op.
        restart = await tasks_dao.mark_started(conn, task_id=t.id)
        assert restart is None
        # And cancel — also no-op.
        canc = await tasks_dao.cancel(conn, user_id=uid, task_id=t.id)
        assert canc is None
        row = await tasks_dao.get_by_id(conn, user_id=uid, task_id=t.id)
        assert row is not None and row.status == "completed"
    finally:
        await conn.close()


async def test_cancel_scoped_to_user():
    """A user can't cancel another user's task."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    alice = await _seed_user(dsn)
    bob = await _seed_user(dsn)
    conn = await _conn()
    try:
        t = await tasks_dao.create(conn, user_id=alice, title="alice's")
        attempted = await tasks_dao.cancel(conn, user_id=bob, task_id=t.id)
        row = await tasks_dao.get_by_id(conn, user_id=alice, task_id=t.id)
    finally:
        await conn.close()
    assert attempted is None
    assert row is not None and row.status == "pending"


async def test_mark_failed_records_reason_and_completed_at():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        t = await tasks_dao.create(conn, user_id=uid, title="x")
        failed = await tasks_dao.mark_failed(
            conn, task_id=t.id, reason="planner crashed",
        )
    finally:
        await conn.close()
    assert failed is not None
    assert failed.status == "failed"
    assert failed.blocking_reason == "planner crashed"
    assert failed.completed_at is not None


# --- fetch_for_task -------------------------------------------------------


async def test_fetch_for_task_returns_chronological():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        t = await tasks_dao.create(conn, user_id=uid, title="x")
        await task_events.append_event(
            conn, task_id=t.id, event_type="status.pending",
        )
        await task_events.append_event(
            conn, task_id=t.id, event_type="status.running",
        )
        await task_events.append_event(
            conn, task_id=t.id, event_type="status.completed",
        )
        events = await task_events.fetch_for_task(conn, task_id=t.id)
    finally:
        await conn.close()
    assert [e.event_type for e in events] == [
        "status.pending", "status.running", "status.completed",
    ]


async def test_get_depth_walks_parent_chain():
    """Subagent depth enforcement (step 16) relies on this."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        root = await tasks_dao.create(conn, user_id=uid, title="root")
        child = await tasks_dao.create(
            conn, user_id=uid, title="child", parent_task_id=root.id,
        )
        grand = await tasks_dao.create(
            conn, user_id=uid, title="grandchild", parent_task_id=child.id,
        )
        assert await tasks_dao.get_depth(conn, task_id=root.id) == 0
        assert await tasks_dao.get_depth(conn, task_id=child.id) == 1
        assert await tasks_dao.get_depth(conn, task_id=grand.id) == 2
    finally:
        await conn.close()


async def test_get_root_walks_to_top_of_chain():
    """`get_root` is used by the executor to scope the per-root subagent
    concurrency cap. Roots return themselves; children return the root."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        root = await tasks_dao.create(conn, user_id=uid, title="root")
        child = await tasks_dao.create(
            conn, user_id=uid, title="child", parent_task_id=root.id,
        )
        grand = await tasks_dao.create(
            conn, user_id=uid, title="grandchild", parent_task_id=child.id,
        )
        assert await tasks_dao.get_root(conn, task_id=root.id) == root.id
        assert await tasks_dao.get_root(conn, task_id=child.id) == root.id
        assert await tasks_dao.get_root(conn, task_id=grand.id) == root.id
    finally:
        await conn.close()


async def test_rollup_spent_cents_sums_token_and_compute_usage():
    """rollup_spent_cents writes the sum of this task's token + compute
    cost rows back to `tasks.spent_cents`. Called by TaskService on
    terminal transitions so the listing reflects authoritative cost."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        t = await tasks_dao.create(conn, user_id=uid, title="cost test")
        # Two token-usage rows + one compute-usage row attributed to this task.
        await conn.execute(
            "INSERT INTO token_usage"
            " (user_id, task_id, agent, model, cost_cents)"
            " VALUES ($1, $2, 'quick', 'claude-haiku-4-5', 17),"
            "        ($1, $2, 'quick', 'claude-haiku-4-5', 13)",
            uid, t.id,
        )
        await conn.execute(
            "INSERT INTO compute_usage"
            " (user_id, task_id, compute_seconds, cost_cents)"
            " VALUES ($1, $2, 5, 7)",
            uid, t.id,
        )
        # A row on a DIFFERENT task must not contribute.
        other = await tasks_dao.create(conn, user_id=uid, title="other")
        await conn.execute(
            "INSERT INTO token_usage"
            " (user_id, task_id, agent, model, cost_cents)"
            " VALUES ($1, $2, 'quick', 'claude-haiku-4-5', 999)",
            uid, other.id,
        )

        total = await tasks_dao.rollup_spent_cents(conn, task_id=t.id)
        row = await tasks_dao.get_by_id(conn, user_id=uid, task_id=t.id)
    finally:
        await conn.close()
    assert total == 17 + 13 + 7
    assert row is not None and row.spent_cents == 17 + 13 + 7


async def test_rollup_spent_cents_writes_zero_when_no_rows():
    """A task that incurred no spend (e.g. cancelled before any model
    call) rolls up to 0 — no rows means the COALESCE kicks in."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        t = await tasks_dao.create(conn, user_id=uid, title="empty")
        total = await tasks_dao.rollup_spent_cents(conn, task_id=t.id)
        row = await tasks_dao.get_by_id(conn, user_id=uid, task_id=t.id)
    finally:
        await conn.close()
    assert total == 0
    assert row is not None and row.spent_cents == 0


async def test_attach_plan_updates_current_plan_id():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    conn = await _conn()
    try:
        t = await tasks_dao.create(conn, user_id=uid, title="x")
        plan_id = await conn.fetchval(
            "INSERT INTO plans (user_id, query, steps) VALUES ($1, 'q', '[]'::jsonb)"
            " RETURNING id",
            uid,
        )
        await tasks_dao.attach_plan(conn, task_id=t.id, plan_id=plan_id)
        row = await tasks_dao.get_by_id(conn, user_id=uid, task_id=t.id)
    finally:
        await conn.close()
    assert row is not None
    assert row.current_plan_id == plan_id
