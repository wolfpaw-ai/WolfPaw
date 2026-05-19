"""Integration test for 001_init.sql.

Skipped when `WOLFPAW_TEST_DATABASE_URL` is unset, so the unit suite stays
green on machines without a local Postgres. Set the env var to a throwaway
database with superuser access (pgvector + pgcrypto extensions need to be
creatable) to exercise this:

    export WOLFPAW_TEST_DATABASE_URL=postgresql://localhost/wolfpaw_test
    uv run pytest tests/test_migrations.py
"""

from __future__ import annotations

import os
from pathlib import Path

import asyncpg
import pytest
from pgvector.asyncpg import register_vector

from wolfpaw.memory.db import apply_sql_file, migrations_dir

pytestmark = pytest.mark.skipif(
    not os.getenv("WOLFPAW_TEST_DATABASE_URL"),
    reason="WOLFPAW_TEST_DATABASE_URL not set",
)

EXPECTED_TABLES = {
    "users", "user_profiles", "user_auth_methods", "api_keys",
    "tier_limits", "subscriptions", "model_prices",
    "threads", "messages", "message_embeddings", "thread_summaries",
    "plans", "skills", "tools",
    "tasks", "task_events", "workspace_files", "sandboxes",
    "prompt_versions", "token_usage", "compute_usage",
    "usage_summaries", "cost_notifications",
}


async def _fresh_db_conn() -> asyncpg.Connection:
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    conn = await asyncpg.connect(dsn=dsn)
    # Wipe public schema for a deterministic start.
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    return conn


async def test_001_init_applies_cleanly():
    conn = await _fresh_db_conn()
    try:
        await apply_sql_file(conn, migrations_dir() / "001_init.sql")
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
        names = {r["tablename"] for r in rows}
        missing = EXPECTED_TABLES - names
        assert not missing, f"missing tables: {missing}"
    finally:
        await conn.close()


async def test_001_init_seeds_tier_limits_and_model_prices():
    conn = await _fresh_db_conn()
    try:
        await apply_sql_file(conn, migrations_dir() / "001_init.sql")
        tiers = {
            r["tier"] for r in await conn.fetch("SELECT tier FROM tier_limits")
        }
        assert {"dev", "self_host", "starter", "pro", "enterprise"}.issubset(tiers)

        models = {
            r["model_id"] for r in await conn.fetch("SELECT model_id FROM model_prices")
        }
        assert {
            "claude-haiku-4-5",
            "claude-sonnet-4-6",
            "claude-opus-4-7",
            "voyage-3",
        }.issubset(models)
    finally:
        await conn.close()


async def test_vector_roundtrip():
    """pgvector adapter registers and a 1024-dim vector survives a roundtrip."""
    conn = await _fresh_db_conn()
    try:
        await apply_sql_file(conn, migrations_dir() / "001_init.sql")
        await register_vector(conn)

        user_id = await conn.fetchval(
            "INSERT INTO users (email) VALUES ($1) RETURNING id",
            "vector@test.local",
        )
        thread_id = await conn.fetchval(
            "INSERT INTO threads (user_id, channel) VALUES ($1, 'web') RETURNING id",
            user_id,
        )
        msg_id = await conn.fetchval(
            "INSERT INTO messages (thread_id, role, content)"
            " VALUES ($1, 'user', 'hi') RETURNING id",
            thread_id,
        )
        vec = [0.1] * 1024
        await conn.execute(
            "INSERT INTO message_embeddings (message_id, embedding) VALUES ($1, $2)",
            msg_id, vec,
        )
        stored = await conn.fetchval(
            "SELECT embedding FROM message_embeddings WHERE message_id = $1",
            msg_id,
        )
        assert len(stored) == 1024
        assert abs(float(stored[0]) - 0.1) < 1e-6
    finally:
        await conn.close()
