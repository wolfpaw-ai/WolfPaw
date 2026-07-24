"""HTTP-level tests for the /monitor endpoints.

These drive the app through httpx's `ASGITransport` rather than Starlette's
`TestClient`. That matters: `TestClient` runs the app in its own event loop
while sharing the process-global asyncpg pool, which is what produces the
`InterfaceError: another operation is in progress` failures and the
`RuntimeError: Event loop is closed` teardowns in the older route-test files.
`ASGITransport` runs the app in *this* test's loop, so the pool is used from
exactly one loop and none of that happens.

No lifespan manager is needed — the pool is created lazily on first
`acquire()`, and the app's startup hook only pre-warms it.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest

from tests.conftest import fresh_db_conn
from wolfpaw.api import create_app
from wolfpaw.auth.deps import require_user_id
from wolfpaw.memory.db import apply_sql_file, migrations_dir

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


def _client(user_id: UUID) -> httpx.AsyncClient:
    app = create_app()
    app.dependency_overrides[require_user_id] = lambda: user_id
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _seed(user_id: UUID, **kw) -> None:
    conn = await asyncpg.connect(dsn=os.environ["WOLFPAW_TEST_DATABASE_URL"])
    try:
        await conn.execute(
            "INSERT INTO users (id, email, email_verified)"
            " VALUES ($1, $2, TRUE) ON CONFLICT (id) DO NOTHING",
            user_id, f"mr+{user_id.hex[:8]}@test.local",
        )
        await conn.execute(
            """
            INSERT INTO model_call_logs
              (run_id, trace_id, user_id, agent, model, status, latency_ms,
               cost_cents, error_type, error_message, system_prompt,
               response_text)
            VALUES (gen_random_uuid(), $1, $2, $3, 'claude-haiku-4-5', $4,
                    $5, $6, $7, $8, 'be brief', 'answer')
            """,
            kw.get("trace_id", "t1"), user_id, kw.get("agent", "quick"),
            kw.get("status", "ok"), kw.get("latency_ms", 50),
            kw.get("cost_cents", 1), kw.get("error_type"),
            kw.get("error_message"),
        )
    finally:
        await conn.close()


async def test_summary_endpoint_reports_error_rate():
    uid = uuid4()
    await _seed(uid, status="ok")
    await _seed(uid, status="error", error_type="APIError",
                error_message="overloaded")
    async with _client(uid) as c:
        r = await c.get("/monitor/summary?window=24h")

    assert r.status_code == 200
    body = r.json()
    assert body["calls"] == 2
    assert body["errors"] == 1
    assert body["error_rate"] == pytest.approx(0.5)
    assert body["top_errors"][0]["error_message"] == "overloaded"


async def test_summary_rejects_an_unknown_window():
    uid = uuid4()
    await _seed(uid)
    async with _client(uid) as c:
        r = await c.get("/monitor/summary?window=5y")
    assert r.status_code == 400
    assert "unknown window" in r.json()["detail"]


async def test_traces_endpoint_lists_and_filters():
    uid = uuid4()
    await _seed(uid, trace_id="clean")
    await _seed(uid, trace_id="broken", status="error",
                error_type="Boom", error_message="x")
    async with _client(uid) as c:
        all_traces = (await c.get("/monitor/traces?window=24h")).json()
        failing = (await c.get("/monitor/traces?window=24h&errors=true")).json()

    assert {t["trace_id"] for t in all_traces} == {"clean", "broken"}
    assert [t["trace_id"] for t in failing] == ["broken"]


async def test_trace_detail_returns_payloads():
    uid = uuid4()
    await _seed(uid, trace_id="deep")
    async with _client(uid) as c:
        r = await c.get("/monitor/traces/deep")

    assert r.status_code == 200
    calls = r.json()
    assert calls[0]["system_prompt"] == "be brief"
    assert calls[0]["response_text"] == "answer"


async def test_trace_detail_404s_for_another_users_trace():
    mine, theirs = uuid4(), uuid4()
    await _seed(theirs, trace_id="secret")
    await _seed(mine, trace_id="mine")
    async with _client(mine) as c:
        r = await c.get("/monitor/traces/secret")

    assert r.status_code == 404


async def test_errors_endpoint_returns_only_failures():
    uid = uuid4()
    await _seed(uid, status="ok")
    await _seed(uid, status="error", error_type="Boom", error_message="x")
    async with _client(uid) as c:
        r = await c.get("/monitor/errors?window=24h")

    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["error_type"] == "Boom"


async def test_limit_is_capped_server_side():
    """A caller asking for 10k rows gets a 422, not a table scan."""
    uid = uuid4()
    await _seed(uid)
    async with _client(uid) as c:
        r = await c.get("/monitor/traces?window=24h&limit=10000")
    assert r.status_code == 422
