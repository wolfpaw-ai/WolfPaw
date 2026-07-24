"""DB-backed tests for the monitoring read layer.

These go through real SQL on purpose — the queries are the product here
(percentiles, error grouping, per-trace rollups), so mocking the database
would test nothing. Skipped without `WOLFPAW_TEST_DATABASE_URL`.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import asyncpg
import pytest

from tests.conftest import fresh_db_conn
from wolfpaw.memory.db import apply_sql_file, migrations_dir
from wolfpaw.metering import monitor

pytestmark = pytest.mark.skipif(
    not os.getenv("WOLFPAW_TEST_DATABASE_URL"),
    reason="WOLFPAW_TEST_DATABASE_URL not set",
)


@pytest.fixture(autouse=True)
async def _db(monkeypatch):
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    monkeypatch.setenv("WOLFPAW_DATABASE_URL", dsn)
    from wolfpaw.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    conn = await fresh_db_conn()
    try:
        for p in sorted(migrations_dir().glob("*.sql")):
            await apply_sql_file(conn, p)
    finally:
        await conn.close()
    yield


async def _conn() -> asyncpg.Connection:
    return await asyncpg.connect(dsn=os.environ["WOLFPAW_TEST_DATABASE_URL"])


async def _seed_user(conn: asyncpg.Connection) -> UUID:
    return await conn.fetchval(
        "INSERT INTO users (email, email_verified) VALUES ($1, TRUE) RETURNING id",
        f"mon+{uuid4().hex[:8]}@test.local",
    )


async def _log(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    agent: str = "quick",
    status: str = "ok",
    trace_id: str = "t1",
    latency_ms: int = 100,
    cost_cents: int = 1,
    error_type: str | None = None,
    error_message: str | None = None,
    created_at: datetime | None = None,
    attempt: int = 1,
) -> None:
    await conn.execute(
        """
        INSERT INTO model_call_logs
          (run_id, trace_id, user_id, agent, model, status, attempt,
           latency_ms, cost_cents, error_type, error_message, created_at,
           response_text, system_prompt)
        VALUES (gen_random_uuid(), $1, $2, $3, 'claude-haiku-4-5', $4, $5,
                $6, $7, $8, $9, $10, 'answer', 'be brief')
        """,
        trace_id, user_id, agent, status, attempt, latency_ms, cost_cents,
        error_type, error_message,
        created_at or datetime.now(timezone.utc),
    )


# --- summary --------------------------------------------------------------


async def test_summary_counts_errors_and_error_rate():
    conn = await _conn()
    try:
        uid = await _seed_user(conn)
        for _ in range(3):
            await _log(conn, user_id=uid, status="ok")
        await _log(
            conn, user_id=uid, status="error",
            error_type="RuntimeError", error_message="overloaded",
        )
        s = await monitor.build_summary(uid, "24h")
    finally:
        await conn.close()

    assert s.calls == 4
    assert s.errors == 1
    assert s.error_rate == pytest.approx(0.25)


async def test_summary_latency_percentiles():
    conn = await _conn()
    try:
        uid = await _seed_user(conn)
        for ms in (10, 20, 30, 40, 1000):
            await _log(conn, user_id=uid, latency_ms=ms)
        s = await monitor.build_summary(uid, "24h")
    finally:
        await conn.close()

    assert s.p50_latency_ms == 30
    # p95 must surface the outlier — the whole point of tracking it next to
    # p50 is that a mean would bury this.
    assert s.p95_latency_ms == 1000


async def test_summary_is_scoped_to_the_window():
    conn = await _conn()
    try:
        uid = await _seed_user(conn)
        await _log(conn, user_id=uid)
        await _log(
            conn, user_id=uid,
            created_at=datetime.now(timezone.utc) - timedelta(hours=5),
        )
        recent = await monitor.build_summary(uid, "1h")
        day = await monitor.build_summary(uid, "24h")
    finally:
        await conn.close()

    assert recent.calls == 1
    assert day.calls == 2


async def test_summary_never_leaks_another_users_calls():
    conn = await _conn()
    try:
        mine = await _seed_user(conn)
        theirs = await _seed_user(conn)
        await _log(conn, user_id=mine)
        for _ in range(5):
            await _log(conn, user_id=theirs, status="error",
                       error_type="Boom", error_message="not yours")
        s = await monitor.build_summary(mine, "24h")
    finally:
        await conn.close()

    assert s.calls == 1
    assert s.errors == 0
    assert s.top_errors == []


async def test_summary_groups_errors_by_type_and_message():
    conn = await _conn()
    try:
        uid = await _seed_user(conn)
        for _ in range(3):
            await _log(conn, user_id=uid, status="error",
                       error_type="APIError", error_message="overloaded")
        await _log(conn, user_id=uid, status="error",
                   error_type="APIError", error_message="bad tool schema")
        s = await monitor.build_summary(uid, "24h")
    finally:
        await conn.close()

    assert [(e.error_message, e.count) for e in s.top_errors] == [
        ("overloaded", 3),
        ("bad tool schema", 1),
    ]


async def test_summary_by_agent_puts_worst_offender_first():
    conn = await _conn()
    try:
        uid = await _seed_user(conn)
        await _log(conn, user_id=uid, agent="quick")
        for _ in range(2):
            await _log(conn, user_id=uid, agent="executor", status="error",
                       error_type="Boom", error_message="x")
        s = await monitor.build_summary(uid, "24h")
    finally:
        await conn.close()

    assert s.by_agent[0].agent == "executor"
    assert s.by_agent[0].errors == 2


# --- traces ---------------------------------------------------------------


async def test_list_traces_rolls_up_calls_per_trace():
    conn = await _conn()
    try:
        uid = await _seed_user(conn)
        await _log(conn, user_id=uid, trace_id="a", agent="triage", cost_cents=1)
        await _log(conn, user_id=uid, trace_id="a", agent="quick", cost_cents=2)
        await _log(conn, user_id=uid, trace_id="b", agent="quick", cost_cents=5)
        rows = await monitor.list_traces(uid, "24h")
    finally:
        await conn.close()

    by_id = {r.trace_id: r for r in rows}
    assert by_id["a"].calls == 2
    assert by_id["a"].cost_cents == 3
    assert by_id["a"].agents == ["quick", "triage"]
    assert by_id["b"].calls == 1


async def test_list_traces_errors_filter_keeps_only_failing_traces():
    conn = await _conn()
    try:
        uid = await _seed_user(conn)
        await _log(conn, user_id=uid, trace_id="clean")
        await _log(conn, user_id=uid, trace_id="broken")
        await _log(conn, user_id=uid, trace_id="broken", status="error",
                   error_type="Boom", error_message="x")
        rows = await monitor.list_traces(uid, "24h", only_errors=True)
    finally:
        await conn.close()

    assert [r.trace_id for r in rows] == ["broken"]
    # The clean call inside the failing trace is still counted.
    assert rows[0].calls == 2 and rows[0].errors == 1


async def test_get_trace_returns_calls_with_payloads_in_order():
    conn = await _conn()
    try:
        uid = await _seed_user(conn)
        now = datetime.now(timezone.utc)
        await _log(conn, user_id=uid, trace_id="z", agent="triage",
                   created_at=now - timedelta(seconds=2))
        await _log(conn, user_id=uid, trace_id="z", agent="quick",
                   created_at=now)
        calls = await monitor.get_trace(uid, "z")
    finally:
        await conn.close()

    assert [c.agent for c in calls] == ["triage", "quick"]
    assert calls[0].system_prompt == "be brief"
    assert calls[0].response_text == "answer"


async def test_get_trace_is_user_scoped():
    conn = await _conn()
    try:
        mine = await _seed_user(conn)
        theirs = await _seed_user(conn)
        await _log(conn, user_id=theirs, trace_id="secret")
        calls = await monitor.get_trace(mine, "secret")
    finally:
        await conn.close()

    assert calls == []


# --- error feed -----------------------------------------------------------


async def test_recent_errors_newest_first_and_only_errors():
    conn = await _conn()
    try:
        uid = await _seed_user(conn)
        now = datetime.now(timezone.utc)
        await _log(conn, user_id=uid, status="ok")
        await _log(conn, user_id=uid, status="error", error_type="Old",
                   error_message="x", created_at=now - timedelta(minutes=5))
        await _log(conn, user_id=uid, status="error", error_type="New",
                   error_message="y", created_at=now)
        rows = await monitor.recent_errors(uid, "24h")
    finally:
        await conn.close()

    assert [r.error_type for r in rows] == ["New", "Old"]


async def test_retry_attempts_are_visible_on_the_error_feed():
    """Attempt numbers are what make a retry policy tunable — a trace with
    three attempts should read as three rows, not one."""
    conn = await _conn()
    try:
        uid = await _seed_user(conn)
        for attempt in (1, 2, 3):
            await _log(conn, user_id=uid, trace_id="retried", status="error",
                       attempt=attempt, error_type="APIError",
                       error_message="overloaded")
        rows = await monitor.recent_errors(uid, "24h")
    finally:
        await conn.close()

    assert sorted(r.attempt for r in rows) == [1, 2, 3]


# --- window validation ----------------------------------------------------


def test_parse_window_rejects_unknown_values():
    with pytest.raises(ValueError, match="unknown window"):
        monitor.parse_window("5y")
