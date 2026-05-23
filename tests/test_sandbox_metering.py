"""Tests for sandbox compute metering.

Unit tests for the pricing math + DB-gated tests for the recorder.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import pytest

from wolfpaw.memory.db import apply_sql_file, close_pool, migrations_dir
from wolfpaw.sandbox.metering import compute_cost_cents, record_compute


# --- unit -------------------------------------------------------------------


def test_compute_cost_rounds_up_never_undercharges():
    # 100 µ¢/s × 1s = 100µ¢ = 0.0001¢ → rounds up to 1¢
    assert compute_cost_cents(1.0, per_second_micros=100) == 1
    # 100 µ¢/s × 5000s = 500_000µ¢ = 0.50¢ → ceil to 1¢
    assert compute_cost_cents(5000.0, per_second_micros=100) == 1
    # 0 seconds → 0 cents
    assert compute_cost_cents(0.0, per_second_micros=100) == 0


def test_compute_cost_with_realistic_rate():
    # 0.10¢/s = 100_000 µ¢/s × 60s = 6_000_000 µ¢ = 6¢
    assert compute_cost_cents(60.0, per_second_micros=100_000) == 6


# --- DB ---------------------------------------------------------------------


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
        for name in ("001_init.sql", "002_auth.sql", "003_sandbox.sql"):
            await apply_sql_file(conn, migrations_dir() / name)
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
            f"sb+{uuid4().hex[:8]}@test.local",
        )
    finally:
        await conn.close()


async def test_record_compute_with_no_task():
    """Migration 003 should let us record ad-hoc compute (task_id NULL)."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    sid = uuid4()
    await record_compute(
        user_id=uid, task_id=None, sandbox_id=sid,
        provider="subprocess", compute_seconds=12.5,
    )
    conn = await asyncpg.connect(dsn=dsn)
    try:
        rows = await conn.fetch(
            "SELECT compute_seconds, cost_cents, task_id"
            "  FROM compute_usage WHERE user_id = $1", uid,
        )
    finally:
        await conn.close()
    assert len(rows) == 1
    assert rows[0]["task_id"] is None
    assert rows[0]["compute_seconds"] == 13  # ceil to int


async def test_record_compute_upserts_sandbox_row():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    sid = uuid4()
    # Two record_compute calls for the same sandbox → second should accumulate.
    await record_compute(
        user_id=uid, task_id=None, sandbox_id=sid,
        provider="subprocess", compute_seconds=10.0,
    )
    await record_compute(
        user_id=uid, task_id=None, sandbox_id=sid,
        provider="subprocess", compute_seconds=5.0,
    )
    conn = await asyncpg.connect(dsn=dsn)
    try:
        row = await conn.fetchrow(
            "SELECT compute_seconds, status::text AS status, provider"
            "  FROM sandboxes WHERE id = $1", sid,
        )
        cu_count = await conn.fetchval(
            "SELECT COUNT(*) FROM compute_usage WHERE sandbox_id = $1", sid,
        )
    finally:
        await conn.close()
    assert row["provider"] == "subprocess"
    assert row["status"] == "stopped"
    assert row["compute_seconds"] == 15
    assert cu_count == 2
