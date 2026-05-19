"""DB-backed tests for pricing lookup, prompt versions, and recorder.

Skipped when `WOLFPAW_TEST_DATABASE_URL` is unset.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import asyncpg
import pytest

from wolfpaw.memory.db import apply_sql_file, close_pool, migrations_dir
from wolfpaw.metering.pricing import compute_cost_cents, get_active_price
from wolfpaw.metering.prompt_versions import (
    bump_prompt_version,
    get_active_prompt_version,
)
from wolfpaw.metering.recorder import record_usage
from wolfpaw.metering.types import TokenCounts

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
        await apply_sql_file(conn, migrations_dir() / "002_auth.sql")
    finally:
        await conn.close()
    await close_pool()
    yield
    await close_pool()


async def _seed_user(dsn: str) -> str:
    conn = await asyncpg.connect(dsn=dsn)
    try:
        return await conn.fetchval(
            "INSERT INTO users (email, email_verified) VALUES ($1, TRUE)"
            " RETURNING id",
            "meter@test.local",
        )
    finally:
        await conn.close()


async def test_get_active_price_returns_seeded_row():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    conn = await asyncpg.connect(dsn=dsn)
    try:
        price = await get_active_price(conn, "claude-haiku-4-5")
    finally:
        await conn.close()
    assert price is not None
    assert price.input_per_mtok_cents == 100
    assert price.output_per_mtok_cents == 500


async def test_get_active_price_returns_none_for_unknown_model():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    conn = await asyncpg.connect(dsn=dsn)
    try:
        price = await get_active_price(conn, "no-such-model-xyz")
    finally:
        await conn.close()
    assert price is None


async def test_get_active_price_honors_effective_window():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    conn = await asyncpg.connect(dsn=dsn)
    try:
        # Insert an older row that ended in 2025 and a newer row.
        await conn.execute(
            "DELETE FROM model_prices WHERE model_id = 'claude-haiku-4-5'"
        )
        await conn.execute(
            "INSERT INTO model_prices (model_id, input_per_mtok_cents,"
            " output_per_mtok_cents, effective_from, effective_to)"
            " VALUES ('claude-haiku-4-5', 200, 1000,"
            "         '2025-01-01', '2026-01-01')"
        )
        await conn.execute(
            "INSERT INTO model_prices (model_id, input_per_mtok_cents,"
            " output_per_mtok_cents, effective_from)"
            " VALUES ('claude-haiku-4-5', 100, 500, '2026-01-01')"
        )
        old = await get_active_price(
            conn, "claude-haiku-4-5",
            at_time=datetime(2025, 6, 1, tzinfo=timezone.utc),
        )
        new = await get_active_price(
            conn, "claude-haiku-4-5",
            at_time=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
    finally:
        await conn.close()
    assert old is not None and old.input_per_mtok_cents == 200
    assert new is not None and new.input_per_mtok_cents == 100


async def test_prompt_version_bump_and_lookup():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    conn = await asyncpg.connect(dsn=dsn)
    try:
        v1 = await bump_prompt_version(
            conn, agent="planner", version_label="v1",
            content_template={"system": "be careful"},
        )
        v2 = await bump_prompt_version(
            conn, agent="planner", version_label="v2",
            content_template={"system": "be VERY careful"},
        )
        active = await get_active_prompt_version(conn, "planner")
    finally:
        await conn.close()
    assert active is not None
    assert active.id == v2.id
    assert v1.content_hash != v2.content_hash


async def test_record_usage_writes_row_with_trace_id():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    user_id = await _seed_user(dsn)
    from wolfpaw.tracing import set_trace_id
    set_trace_id("trace-abc-123")
    try:
        row_id = await record_usage(
            user_id=user_id,
            agent="quick",
            model="claude-haiku-4-5",
            usage=TokenCounts(input_tokens=1000, output_tokens=200),
            cost_cents=42,
        )
    finally:
        set_trace_id(None)

    conn = await asyncpg.connect(dsn=dsn)
    try:
        row = await conn.fetchrow(
            "SELECT trace_id, cost_cents, input_tokens, agent::text AS agent"
            " FROM token_usage WHERE id = $1", row_id,
        )
    finally:
        await conn.close()
    assert row["trace_id"] == "trace-abc-123"
    assert row["cost_cents"] == 42
    assert row["input_tokens"] == 1000
    assert row["agent"] == "quick"


async def test_end_to_end_pricing_then_record():
    """Tie pricing + record_usage together against real DB rows."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    user_id = await _seed_user(dsn)
    usage = TokenCounts(input_tokens=2_000_000, output_tokens=500_000)
    conn = await asyncpg.connect(dsn=dsn)
    try:
        price = await get_active_price(conn, "claude-sonnet-4-6")
    finally:
        await conn.close()
    assert price is not None
    cost = compute_cost_cents(price, usage)
    assert cost == 600 + 750  # input 2M * 300/Mtok + output 500K * 1500/Mtok

    row_id = await record_usage(
        user_id=user_id,
        agent="planner",
        model="claude-sonnet-4-6",
        usage=usage,
        cost_cents=cost,
    )
    conn = await asyncpg.connect(dsn=dsn)
    try:
        row = await conn.fetchrow(
            "SELECT cost_cents FROM token_usage WHERE id = $1", row_id
        )
    finally:
        await conn.close()
    assert row["cost_cents"] == cost
