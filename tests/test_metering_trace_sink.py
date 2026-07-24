"""Unit tests for the Postgres trace sink.

The DB is mocked — these cover the sink's own contract: what it puts on the
row, that failures are captured rather than swallowed, that oversized payloads
are clipped to valid JSON, and that a write error can never propagate into the
model call it was observing.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from wolfpaw.metering.trace_sink import (
    NullTraceSink,
    PostgresTraceSink,
    _dump_capped,
    get_parent_run_id,
)
from wolfpaw.metering.types import TokenCounts


class _CaptureConn:
    """Records the parameters of every execute() so assertions can read the
    row that would have been inserted."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple] = []
        self._fail = fail

    async def execute(self, sql: str, *args):
        if self._fail:
            raise RuntimeError("db is down")
        self.calls.append((sql, args))


@pytest.fixture
def capture(monkeypatch):
    conn = _CaptureConn()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr("wolfpaw.metering.trace_sink.acquire", lambda: ctx)
    return conn


def _row(conn: _CaptureConn) -> dict:
    """Map the positional INSERT args back onto column names."""
    columns = [
        "run_id", "parent_run_id", "trace_id", "request_id", "user_id",
        "task_id", "token_usage_id", "agent", "model", "prompt_version_id",
        "system_prompt", "request_messages", "request_params",
        "response_content", "response_text", "stop_reason",
        "input_tokens", "output_tokens", "cache_read_tokens",
        "cache_write_tokens", "cost_cents", "latency_ms", "attempt",
        "status", "error_type", "error_message", "truncated",
    ]
    assert conn.calls, "no row was written"
    return dict(zip(columns, conn.calls[-1][1]))


async def test_successful_run_writes_ok_row(capture):
    sink = PostgresTraceSink(max_payload_bytes=10_000)
    user_id = uuid4()
    raw = SimpleNamespace(
        content=[{"type": "text", "text": "hi"}], stop_reason="end_turn"
    )

    async with sink.trace(
        user_id=user_id,
        agent="quick",
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "hello"}],
        system="be brief",
    ) as run:
        run.mark_response(raw, text="hi")
        run.mark_cost(
            usage=TokenCounts(input_tokens=7, output_tokens=3),
            cost_cents=2,
            token_usage_id=None,
        )

    row = _row(capture)
    assert row["status"] == "ok"
    assert row["agent"] == "quick"
    assert row["user_id"] == user_id
    assert row["response_text"] == "hi"
    assert row["stop_reason"] == "end_turn"
    assert row["input_tokens"] == 7
    assert row["cost_cents"] == 2
    assert row["system_prompt"] == "be brief"
    assert json.loads(row["request_messages"])[0]["content"] == "hello"
    assert row["latency_ms"] is not None
    assert row["truncated"] is False


async def test_failed_call_is_recorded_and_reraised(capture):
    """The whole point of the table: a call that raises still leaves a row.
    `token_usage` never sees this call at all."""
    sink = PostgresTraceSink(max_payload_bytes=10_000)

    with pytest.raises(ValueError, match="overloaded"):
        async with sink.trace(
            user_id=uuid4(), agent="planner", model="claude-sonnet-4-6"
        ):
            raise ValueError("overloaded")

    row = _row(capture)
    assert row["status"] == "error"
    assert row["error_type"] == "ValueError"
    assert "overloaded" in row["error_message"]
    assert row["latency_ms"] is not None


async def test_attempt_number_is_recorded(capture):
    sink = PostgresTraceSink(max_payload_bytes=10_000)
    async with sink.trace(
        user_id=uuid4(), agent="quick", model="m", attempt=3
    ):
        pass
    assert _row(capture)["attempt"] == 3


async def test_nested_call_links_to_parent(capture):
    """Sub-agent calls made inside an open run become its children, without
    any call site plumbing a run id through."""
    sink = PostgresTraceSink(max_payload_bytes=10_000)
    user_id = uuid4()

    async with sink.trace(user_id=user_id, agent="executor", model="m") as outer:
        assert get_parent_run_id() == outer.run_id
        async with sink.trace(user_id=user_id, agent="quick", model="m") as inner:
            pass
        child = _row(capture)

    assert child["parent_run_id"] == outer.run_id
    assert child["run_id"] == inner.run_id
    # Outer wrote last and has no parent of its own.
    assert _row(capture)["parent_run_id"] is None


async def test_context_is_restored_after_run(capture):
    sink = PostgresTraceSink(max_payload_bytes=10_000)
    async with sink.trace(user_id=uuid4(), agent="quick", model="m"):
        pass
    assert get_parent_run_id() is None


async def test_oversized_payload_is_clipped_to_valid_json(capture):
    sink = PostgresTraceSink(max_payload_bytes=200)
    async with sink.trace(
        user_id=uuid4(),
        agent="quick",
        model="m",
        messages=[{"role": "user", "content": "x" * 5000}],
    ):
        pass

    row = _row(capture)
    assert row["truncated"] is True
    # Must still parse — a naively sliced JSON string would break the UI.
    parsed = json.loads(row["request_messages"])
    assert parsed["_truncated"] is True
    assert parsed["_bytes"] > 5000


async def test_write_failure_does_not_break_the_model_call(monkeypatch):
    """Observability must never be able to take down what it observes."""
    conn = _CaptureConn(fail=True)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr("wolfpaw.metering.trace_sink.acquire", lambda: ctx)

    sink = PostgresTraceSink(max_payload_bytes=10_000)
    async with sink.trace(user_id=uuid4(), agent="quick", model="m") as run:
        run.mark_response(SimpleNamespace(content=[], stop_reason="end_turn"))
    # No exception — that is the assertion.


async def test_null_sink_yields_a_run_and_writes_nothing():
    sink = NullTraceSink()
    async with sink.trace(user_id=uuid4(), agent="quick", model="m") as run:
        run.mark_response(SimpleNamespace(content=[], stop_reason=None), text="x")
    assert run.status == "ok"


def test_dump_capped_marks_unserializable_payloads():
    class Exploding:
        def __repr__(self):  # pragma: no cover - exercised via json.dumps
            return "<exploding>"

    text, truncated = _dump_capped({"k": {1, 2, 3}}, 10_000)
    # A set isn't JSON-serializable but degrades to repr rather than raising.
    assert truncated is False
    assert "1" in text
