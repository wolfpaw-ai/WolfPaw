"""Wrapper-shape tests for `ModelClient.call`.

We mock the Anthropic client AND the DB-bound calls (pricing + recorder) so
this test runs without a real Postgres or Anthropic key.

The integration test in test_metering_integration.py exercises the same
wrapper against a real DB.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from wolfpaw.metering.model_client import ModelClient
from wolfpaw.metering.trace_sink import ModelRun
from wolfpaw.metering.types import ModelPrice, TokenCounts


def _fake_anthropic_response(input_tokens: int = 10, output_tokens: int = 5):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hello")],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


class _FakeAnthropic:
    def __init__(self, response) -> None:
        self.messages = SimpleNamespace(create=AsyncMock(return_value=response))


@pytest.fixture
def _mock_db(monkeypatch):
    """Stub the DB-bound helpers used by the wrapper."""
    price = ModelPrice(
        model_id="claude-sonnet-4-6",
        input_per_mtok_cents=300,
        output_per_mtok_cents=1500,
        cache_read_per_mtok_cents=30,
        cache_write_per_mtok_cents=375,
        effective_from=__import__("datetime").datetime(
            2026, 1, 1, tzinfo=__import__("datetime").timezone.utc
        ),
    )
    # Patch pricing lookup and recorder insert at the module-of-use.
    with patch("wolfpaw.metering.model_client.get_active_price",
               new=AsyncMock(return_value=price)) as p, \
         patch("wolfpaw.metering.model_client.record_usage",
               new=AsyncMock(return_value=uuid4())) as r, \
         patch("wolfpaw.metering.model_client.acquire") as a:
        # acquire() is used as an async context manager; we just need it to
        # yield something — the patched pricing/recorder don't touch it.
        a.return_value.__aenter__ = AsyncMock(return_value=None)
        a.return_value.__aexit__ = AsyncMock(return_value=None)
        yield {"pricing": p, "recorder": r}


async def test_call_records_usage_and_returns_text(_mock_db):
    client = ModelClient(anthropic=_FakeAnthropic(_fake_anthropic_response()))
    result = await client.call(
        user_id=uuid4(),
        agent="quick",
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert result.text == "hello"
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 5
    assert result.cost_cents >= 1  # 10*300/1e6 + 5*1500/1e6 = 0.0105c → ceil 1
    _mock_db["recorder"].assert_awaited_once()


async def test_call_passes_through_max_tokens_and_system(_mock_db):
    fake = _FakeAnthropic(_fake_anthropic_response())
    client = ModelClient(anthropic=fake)
    await client.call(
        user_id=uuid4(),
        agent="planner",
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "plan this"}],
        system="You are Wolfpaw.",
        max_tokens=4096,
    )
    fake.messages.create.assert_awaited_once()
    kwargs = fake.messages.create.await_args.kwargs
    assert kwargs["model"] == "claude-sonnet-4-6"
    assert kwargs["max_tokens"] == 4096
    assert kwargs["system"] == "You are Wolfpaw."


async def test_missing_price_logs_warning_and_costs_zero(_mock_db):
    _mock_db["pricing"].return_value = None
    client = ModelClient(anthropic=_FakeAnthropic(_fake_anthropic_response()))
    result = await client.call(
        user_id=uuid4(),
        agent="triage",
        model="some-unseeded-model",
        messages=[{"role": "user", "content": "?"}],
    )
    assert result.cost_cents == 0
    _mock_db["recorder"].assert_awaited_once()


async def test_call_propagates_anthropic_errors(_mock_db):
    fake = _FakeAnthropic(_fake_anthropic_response())
    fake.messages.create = AsyncMock(side_effect=RuntimeError("boom"))
    client = ModelClient(anthropic=fake)
    with pytest.raises(RuntimeError, match="boom"):
        await client.call(
            user_id=uuid4(),
            agent="quick",
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": "hi"}],
        )
    # Recorder must NOT fire on failure — we don't bill for failed calls.
    _mock_db["recorder"].assert_not_awaited()


class _RecordingSink:
    """Captures the runs a call produces, so tests can assert on what the
    trace sink would have written without touching Postgres."""

    def __init__(self) -> None:
        self.runs: list = []

    @asynccontextmanager
    async def trace(self, **kwargs):
        run = ModelRun(
            run_id=uuid4(),
            parent_run_id=None,
            user_id=kwargs["user_id"],
            agent=kwargs["agent"],
            model=kwargs["model"],
            request_params=kwargs.get("params") or {},
        )
        self.runs.append(run)
        try:
            yield run
        except BaseException as exc:
            run.mark_error(exc)
            raise


async def test_failed_call_produces_an_errored_run(_mock_db):
    """`token_usage` sees nothing when a call fails; the trace run is the
    only record that the attempt happened at all."""
    fake = _FakeAnthropic(_fake_anthropic_response())
    fake.messages.create = AsyncMock(side_effect=RuntimeError("overloaded"))
    sink = _RecordingSink()
    client = ModelClient(anthropic=fake, traces=sink)

    with pytest.raises(RuntimeError):
        await client.call(
            user_id=uuid4(),
            agent="quick",
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": "hi"}],
        )

    _mock_db["recorder"].assert_not_awaited()
    assert len(sink.runs) == 1
    assert sink.runs[0].status == "error"
    assert sink.runs[0].error_type == "RuntimeError"


async def test_successful_call_populates_the_run(_mock_db):
    sink = _RecordingSink()
    client = ModelClient(anthropic=_FakeAnthropic(_fake_anthropic_response()), traces=sink)
    await client.call(
        user_id=uuid4(),
        agent="quick",
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=512,
    )
    run = sink.runs[0]
    assert run.status == "ok"
    assert run.response_text == "hello"
    assert run.usage.input_tokens == 10
    assert run.cost_cents >= 1
    assert run.token_usage_id is not None
    assert run.latency_ms is not None
    # Params ride along, minus the bulky fields that have their own columns.
    assert run.request_params["max_tokens"] == 512
    assert "messages" not in run.request_params


async def test_tool_schemas_are_reduced_to_names(_mock_db):
    """Full tool schemas are large, static, and identical across every call
    by an agent — only the names are worth storing per row."""
    sink = _RecordingSink()
    client = ModelClient(anthropic=_FakeAnthropic(_fake_anthropic_response()), traces=sink)
    await client.call(
        user_id=uuid4(),
        agent="executor",
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "http_get", "input_schema": {"big": "x" * 5000}}],
    )
    params = sink.runs[0].request_params
    assert params["tool_names"] == ["http_get"]
    assert "tools" not in params
