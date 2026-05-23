"""DB-backed tests for `/usage` — the LATERAL-join model breakdown, by-agent
rollup, today/month/all windows, and the 30s cache.

Skipped when WOLFPAW_TEST_DATABASE_URL is unset (same convention as the
other integration suites).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import UUID, uuid4

import asyncpg
import pytest

# Import for the registration side effect (so /usage is on the dispatcher).
import wolfpaw.metering.usage_report  # noqa: F401
from wolfpaw.channels import InboundMessage
from wolfpaw.channels.commands import get_dispatcher
from wolfpaw.memory.db import apply_sql_file, close_pool, migrations_dir
from wolfpaw.metering.usage_report import (
    build_usage_report,
    clear_cache,
)

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
    clear_cache()
    yield
    await close_pool()
    clear_cache()


async def _seed_user(dsn: str, *, tz: str = "UTC") -> UUID:
    conn = await asyncpg.connect(dsn=dsn)
    try:
        uid = await conn.fetchval(
            "INSERT INTO users (email, email_verified) VALUES ($1, TRUE)"
            " RETURNING id",
            f"usage+{uuid4().hex[:8]}@test.local",
        )
        await conn.execute(
            "INSERT INTO user_profiles (user_id, timezone) VALUES ($1, $2)",
            uid,
            tz,
        )
        return uid
    finally:
        await conn.close()


async def _record(
    dsn: str,
    user_id: UUID,
    *,
    agent: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_cents: int,
    created_at: datetime | None = None,
) -> None:
    conn = await asyncpg.connect(dsn=dsn)
    try:
        if created_at is None:
            await conn.execute(
                "INSERT INTO token_usage"
                " (user_id, agent, model, input_tokens, output_tokens, cost_cents)"
                " VALUES ($1, $2::agent_kind, $3, $4, $5, $6)",
                user_id, agent, model, input_tokens, output_tokens, cost_cents,
            )
        else:
            await conn.execute(
                "INSERT INTO token_usage"
                " (user_id, agent, model, input_tokens, output_tokens,"
                "  cost_cents, created_at)"
                " VALUES ($1, $2::agent_kind, $3, $4, $5, $6, $7)",
                user_id, agent, model, input_tokens, output_tokens,
                cost_cents, created_at,
            )
    finally:
        await conn.close()


async def test_report_empty_state_for_new_user():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    report = await build_usage_report(uid, "default")
    assert len(report.periods) == 2  # current period + today
    for p in report.periods:
        assert p.models == []
        assert p.compute_cost_cents == 0
        assert p.total_cost_cents == 0


async def test_report_default_groups_by_model_using_seeded_prices():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    # Insert with cost_cents matching what record_usage would write at the
    # seeded prices; the LATERAL-join re-derive should produce the same totals.
    await _record(dsn, uid, agent="quick",   model="claude-haiku-4-5",
                  input_tokens=1_000_000, output_tokens=200_000, cost_cents=200)
    await _record(dsn, uid, agent="quick",   model="claude-haiku-4-5",
                  input_tokens=500_000,   output_tokens=100_000, cost_cents=100)
    await _record(dsn, uid, agent="planner", model="claude-sonnet-4-6",
                  input_tokens=2_000_000, output_tokens=500_000, cost_cents=1350)

    report = await build_usage_report(uid, "default")
    current_period = report.periods[0]
    models = {m.model: m for m in current_period.models}
    assert set(models) == {"claude-haiku-4-5", "claude-sonnet-4-6"}

    haiku = models["claude-haiku-4-5"]
    assert haiku.input_tokens == 1_500_000
    assert haiku.output_tokens == 300_000
    # Haiku seeded at $1/Mtok in, $5/Mtok out → 150¢ + 150¢ = 300¢
    assert haiku.input_cost_cents == 150
    assert haiku.output_cost_cents == 150
    assert haiku.total_cost_cents == 300

    sonnet = models["claude-sonnet-4-6"]
    # Sonnet seeded at $3/Mtok in, $15/Mtok out → 600¢ + 750¢ = 1350¢
    assert sonnet.total_cost_cents == 1350

    # Most expensive model sorts first.
    assert current_period.models[0].model == "claude-sonnet-4-6"


async def test_report_today_filters_by_user_local_day():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn, tz="UTC")
    now = datetime.now(timezone.utc)
    long_ago = now.replace(year=now.year - 1)
    await _record(dsn, uid, agent="quick", model="claude-haiku-4-5",
                  input_tokens=1_000_000, output_tokens=0, cost_cents=100,
                  created_at=long_ago)
    await _record(dsn, uid, agent="quick", model="claude-haiku-4-5",
                  input_tokens=2_000_000, output_tokens=0, cost_cents=200)

    report = await build_usage_report(uid, "today")
    assert len(report.periods) == 1
    period = report.periods[0]
    assert len(period.models) == 1
    assert period.models[0].input_tokens == 2_000_000


async def test_report_month_includes_by_agent_breakdown():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    await _record(dsn, uid, agent="quick",   model="claude-haiku-4-5",
                  input_tokens=1_000_000, output_tokens=0, cost_cents=100)
    await _record(dsn, uid, agent="planner", model="claude-sonnet-4-6",
                  input_tokens=1_000_000, output_tokens=0, cost_cents=300)

    report = await build_usage_report(uid, "month")
    assert report.include_by_agent is True
    period = report.periods[0]
    agents = {row.agent: row.cost_cents for row in period.by_agent}
    assert agents == {"quick": 100, "planner": 300}


async def test_report_all_covers_everything():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    old = datetime(2024, 1, 1, tzinfo=timezone.utc)
    await _record(dsn, uid, agent="quick", model="claude-haiku-4-5",
                  input_tokens=1_000_000, output_tokens=0, cost_cents=100,
                  created_at=old)
    report = await build_usage_report(uid, "all")
    assert len(report.periods) == 1
    assert report.periods[0].models[0].input_tokens == 1_000_000


async def test_report_cache_returns_same_object_within_ttl():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    await _record(dsn, uid, agent="quick", model="claude-haiku-4-5",
                  input_tokens=1_000_000, output_tokens=0, cost_cents=100)
    first = await build_usage_report(uid, "default")
    # Insert another row — without cache invalidation the report shouldn't change.
    await _record(dsn, uid, agent="quick", model="claude-haiku-4-5",
                  input_tokens=999_000_000, output_tokens=0, cost_cents=99999)
    second = await build_usage_report(uid, "default")
    assert first is second
    clear_cache()
    third = await build_usage_report(uid, "default")
    haiku = next(
        m for m in third.periods[0].models if m.model == "claude-haiku-4-5"
    )
    assert haiku.input_tokens == 1_000_000_000


async def test_usage_command_through_dispatcher_returns_text():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    msg = InboundMessage(user_id=uid, content="/usage", channel_name="web")
    result = await get_dispatcher().dispatch(msg)
    assert result is not None
    assert "Current period" in result.text
    assert "Today" in result.text


async def test_usage_command_rejects_bad_scope_inline():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    msg = InboundMessage(
        user_id=uid, content="/usage weekly", channel_name="web"
    )
    result = await get_dispatcher().dispatch(msg)
    assert result is not None
    assert "weekly" in result.text
