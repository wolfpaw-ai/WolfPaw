"""Unit tests for the schedules layer — pure scheduling math, the
post-fire advance logic, dispatcher rendering, and tool registration.

These avoid a DB: ``compute_next_run_at`` / ``next_after_fire`` /
``_render_content`` are pure functions, and registration just checks the
global registry + quick allowlist.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

import wolfpaw.toolbox  # noqa: F401 — trigger tool registration
from wolfpaw.memory import schedules as sched
from wolfpaw.workers.jobs.dispatch_schedules import _render_content


def _make(**overrides):
    base = dict(
        id=uuid4(),
        user_id=uuid4(),
        instruction="do the thing",
        context={},
        state={},
        recurrence="interval",
        cron_expr=None,
        interval_seconds=600,
        timezone="UTC",
        next_run_at=datetime(2026, 6, 27, 21, 0, tzinfo=timezone.utc),
        last_run_at=None,
        run_count=0,
        max_runs=None,
        until=None,
        channel="telegram",
        status="active",
        title="t",
    )
    base.update(overrides)
    return sched.Schedule(**base)


# --- compute_next_run_at ---------------------------------------------------


def test_once_has_no_next():
    now = datetime(2026, 6, 27, 21, 0, tzinfo=timezone.utc)
    assert sched.compute_next_run_at(recurrence="once", after=now) is None


def test_interval_adds_seconds():
    now = datetime(2026, 6, 27, 21, 0, tzinfo=timezone.utc)
    nxt = sched.compute_next_run_at(
        recurrence="interval", after=now, interval_seconds=600,
    )
    assert nxt == now + timedelta(seconds=600)


def test_interval_skips_overdue_backlog():
    # `after` is "now"; even if many slots were missed, we get exactly one
    # interval past now — no catch-up burst.
    now = datetime(2026, 6, 27, 23, 59, tzinfo=timezone.utc)
    nxt = sched.compute_next_run_at(
        recurrence="interval", after=now, interval_seconds=600,
    )
    assert nxt == now + timedelta(seconds=600)


def test_cron_evaluated_in_timezone():
    # 08:00 daily in New York. From 21:00 UTC (17:00 ET) the next 8am ET is
    # the following morning = 12:00 UTC (EDT, summer).
    after = datetime(2026, 6, 27, 21, 0, tzinfo=timezone.utc)
    nxt = sched.compute_next_run_at(
        recurrence="cron", after=after, cron_expr="0 8 * * *",
        timezone_name="America/New_York",
    )
    assert nxt == datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)


def test_naive_after_rejected():
    with pytest.raises(ValueError):
        sched.compute_next_run_at(
            recurrence="interval", after=datetime(2026, 6, 27, 21, 0),
            interval_seconds=600,
        )


# --- next_after_fire -------------------------------------------------------


def test_once_goes_done_after_fire():
    now = datetime(2026, 6, 27, 21, 0, tzinfo=timezone.utc)
    nxt, status = sched.next_after_fire(_make(recurrence="once"), now=now)
    assert nxt is None and status == "done"


def test_interval_stays_active():
    now = datetime(2026, 6, 27, 21, 0, tzinfo=timezone.utc)
    nxt, status = sched.next_after_fire(_make(), now=now)
    assert status == "active"
    assert nxt == now + timedelta(seconds=600)


def test_max_runs_terminates():
    now = datetime(2026, 6, 27, 21, 0, tzinfo=timezone.utc)
    # run_count=2, max_runs=3 → this firing is the 3rd → done.
    nxt, status = sched.next_after_fire(
        _make(run_count=2, max_runs=3), now=now,
    )
    assert nxt is None and status == "done"


def test_until_terminates():
    now = datetime(2026, 6, 27, 21, 0, tzinfo=timezone.utc)
    nxt, status = sched.next_after_fire(
        _make(until=now + timedelta(seconds=60)), now=now,  # next slot is +600s > until
    )
    assert nxt is None and status == "done"


# --- dispatcher rendering --------------------------------------------------


def test_render_content_includes_instruction_context_state_and_id():
    s = _make(
        instruction="check the weather",
        context={"location": "Brooklyn, NY"},
        state={"last_notified_for": "2026-06-27T22:00"},
    )
    content = _render_content(s)
    assert "check the weather" in content
    assert "Brooklyn, NY" in content
    assert "last_notified_for" in content
    assert str(s.id) in content
    assert "update_schedule_state" in content


# --- registration ----------------------------------------------------------


def test_schedule_tools_registered():
    from wolfpaw.toolbox.registry import get_registry

    names = set(get_registry().names())
    assert {
        "schedule_task", "list_schedules", "cancel_schedule",
        "update_schedule_state",
    } <= names


def test_quick_agent_exposes_user_schedule_tools():
    from wolfpaw.agents.quick import QuickAgent

    allowed = QuickAgent.ALLOWED_TOOL_NAMES
    assert {"schedule_task", "list_schedules", "cancel_schedule"} <= allowed
    # state-write is run-only, not a user-facing quick tool
    assert "update_schedule_state" not in allowed
