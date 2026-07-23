"""Tests for `model_call_logs` retention.

The partition-name math is unit-tested; the actual create/drop cycle needs a
real Postgres and is skipped without `WOLFPAW_TEST_DATABASE_URL` (see
test_migrations.py for how to point that at a throwaway pgvector container).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import asyncpg
import pytest

from tests.conftest import fresh_db_conn
from wolfpaw.memory.db import apply_sql_file, migrations_dir
from wolfpaw.workers.jobs.prune_traces import _partition_start, prune_traces


def test_partition_start_parses_generated_names():
    assert _partition_start("model_call_logs_2026_07") == datetime(
        2026, 7, 1, tzinfo=timezone.utc
    )


def test_partition_start_ignores_foreign_names():
    """Anything we didn't generate is left alone rather than dropped on a
    guess — a hand-created partition is someone's deliberate act."""
    assert _partition_start("model_call_logs_default") is None
    assert _partition_start("token_usage") is None
    assert _partition_start("model_call_logs_2026_13x") is None


# --- DB-backed ------------------------------------------------------------

pytestmark_db = pytest.mark.skipif(
    not os.getenv("WOLFPAW_TEST_DATABASE_URL"),
    reason="WOLFPAW_TEST_DATABASE_URL not set",
)


async def _migrated_conn() -> asyncpg.Connection:
    conn = await fresh_db_conn()
    for name in sorted(p.name for p in migrations_dir().glob("*.sql")):
        await apply_sql_file(conn, migrations_dir() / name)
    return conn


async def _partitions(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch(
        """
        SELECT c.relname AS name
          FROM pg_class c
          JOIN pg_inherits i ON i.inhrelid = c.oid
          JOIN pg_class p ON p.oid = i.inhparent
         WHERE p.relname = 'model_call_logs'
        """
    )
    return {r["name"] for r in rows}


@pytestmark_db
async def test_migration_creates_a_writable_partitioned_table():
    conn = await _migrated_conn()
    try:
        # Three seeded months: last, current, next.
        assert len(await _partitions(conn)) == 3
        user_id = await conn.fetchval(
            "INSERT INTO users (email) VALUES ('t@example.com') RETURNING id"
        )
        await conn.execute(
            "INSERT INTO model_call_logs (run_id, user_id, agent, model)"
            " VALUES (gen_random_uuid(), $1, 'quick', 'claude-haiku-4-5')",
            user_id,
        )
        assert await conn.fetchval("SELECT count(*) FROM model_call_logs") == 1
    finally:
        await conn.close()


@pytestmark_db
async def test_prune_drops_only_aged_out_partitions(monkeypatch):
    conn = await _migrated_conn()
    try:
        # An ancient partition that must go, plus the seeded recent ones.
        await conn.execute("SELECT ensure_model_call_log_partition('2020-01-15')")
        assert "model_call_logs_2020_01" in await _partitions(conn)

        monkeypatch.setattr(
            "wolfpaw.workers.jobs.prune_traces.acquire",
            lambda: _conn_ctx(conn),
        )
        result = await prune_traces(now=datetime.now(timezone.utc))

        assert "model_call_logs_2020_01" in result.dropped
        remaining = await _partitions(conn)
        assert "model_call_logs_2020_01" not in remaining
        # Current month survives, and next month is provisioned.
        assert len(remaining) >= 2
    finally:
        await conn.close()


class _conn_ctx:
    """Adapt a bare asyncpg connection to the `acquire()` context manager."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return None
