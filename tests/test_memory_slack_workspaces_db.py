"""DB-gated tests for `memory.slack_workspaces` DAO."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import pytest

from wolfpaw.memory import slack_workspaces
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
            "006_telegram.sql", "007_slack.sql",
        ):
            await apply_sql_file(conn, migrations_dir() / f)
    finally:
        await conn.close()
    await close_pool()
    yield
    await close_pool()


async def _conn():
    return await asyncpg.connect(dsn=os.environ["WOLFPAW_TEST_DATABASE_URL"])


async def _seed_user() -> UUID:
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    conn = await asyncpg.connect(dsn=dsn)
    try:
        return await conn.fetchval(
            "INSERT INTO users (email, email_verified) VALUES ($1, TRUE) RETURNING id",
            f"slack+{uuid4().hex[:8]}@test.local",
        )
    finally:
        await conn.close()


async def test_upsert_creates_workspace_then_get_returns_it():
    user_id = await _seed_user()
    conn = await _conn()
    try:
        ws = await slack_workspaces.upsert(
            conn,
            team_id="T123", team_name="Acme", bot_user_id="U_BOT",
            bot_token="xoxb-1", installed_by_user_id=user_id,
        )
        fetched = await slack_workspaces.get(conn, team_id="T123")
    finally:
        await conn.close()
    assert ws.team_id == "T123"
    assert ws.bot_token == "xoxb-1"
    assert fetched is not None
    assert fetched.bot_token == "xoxb-1"
    assert fetched.installed_by_user_id == user_id


async def test_upsert_rotates_token_on_reinstall():
    """Re-installing the same workspace overwrites the bot token + records
    the new installer."""
    user1 = await _seed_user()
    user2 = await _seed_user()
    conn = await _conn()
    try:
        await slack_workspaces.upsert(
            conn, team_id="T1", team_name="Acme", bot_user_id="U_BOT_A",
            bot_token="xoxb-old", installed_by_user_id=user1,
        )
        await slack_workspaces.upsert(
            conn, team_id="T1", team_name="Acme Inc.", bot_user_id="U_BOT_B",
            bot_token="xoxb-new", installed_by_user_id=user2,
        )
        ws = await slack_workspaces.get(conn, team_id="T1")
    finally:
        await conn.close()
    assert ws is not None
    assert ws.bot_token == "xoxb-new"
    assert ws.bot_user_id == "U_BOT_B"
    assert ws.team_name == "Acme Inc."
    assert ws.installed_by_user_id == user2


async def test_get_returns_none_for_unknown_team():
    conn = await _conn()
    try:
        ws = await slack_workspaces.get(conn, team_id="T_DOES_NOT_EXIST")
    finally:
        await conn.close()
    assert ws is None


async def test_revoke_hides_workspace_from_get_but_keeps_row():
    user_id = await _seed_user()
    conn = await _conn()
    try:
        await slack_workspaces.upsert(
            conn, team_id="T1", team_name="Acme", bot_user_id="U_BOT",
            bot_token="xoxb-1", installed_by_user_id=user_id,
        )
        await slack_workspaces.revoke(conn, team_id="T1")
        # get() filters revoked rows.
        assert await slack_workspaces.get(conn, team_id="T1") is None
        # The row itself still exists for audit.
        row_count = await conn.fetchval(
            "SELECT COUNT(*) FROM slack_workspaces WHERE team_id = $1", "T1",
        )
    finally:
        await conn.close()
    assert row_count == 1


async def test_reinstall_after_revoke_clears_revoked_at():
    """A previously-revoked workspace becomes active again on re-install."""
    user_id = await _seed_user()
    conn = await _conn()
    try:
        await slack_workspaces.upsert(
            conn, team_id="T1", team_name="Acme", bot_user_id="U_BOT",
            bot_token="xoxb-1", installed_by_user_id=user_id,
        )
        await slack_workspaces.revoke(conn, team_id="T1")
        await slack_workspaces.upsert(
            conn, team_id="T1", team_name="Acme", bot_user_id="U_BOT",
            bot_token="xoxb-2", installed_by_user_id=user_id,
        )
        ws = await slack_workspaces.get(conn, team_id="T1")
    finally:
        await conn.close()
    assert ws is not None
    assert ws.revoked_at is None
    assert ws.bot_token == "xoxb-2"
