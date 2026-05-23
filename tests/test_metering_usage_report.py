"""Pure-unit tests for `/usage`: scope parsing, period-window math, and the
text renderer. DB-backed tests live in `test_metering_usage_report_db.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

# Importing the module registers `/usage` with the dispatcher — keep the
# import so the side effect runs and the slash command is callable.
import wolfpaw.metering.usage_report  # noqa: F401
from wolfpaw.metering.usage_report import (
    AgentRow,
    ModelRow,
    PeriodReport,
    UsageReport,
    _calendar_month_window,
    _format_period_label,
    _today_window,
    parse_scope,
    render_text,
)


def test_parse_scope_defaults_to_default():
    assert parse_scope("") == "default"
    assert parse_scope("   ") == "default"


def test_parse_scope_accepts_known_scopes():
    assert parse_scope("today") == "today"
    assert parse_scope("MONTH") == "month"
    assert parse_scope("all") == "all"


def test_parse_scope_rejects_unknown():
    with pytest.raises(ValueError) as exc:
        parse_scope("weekly")
    assert "weekly" in str(exc.value)


def test_today_window_in_utc():
    now = datetime(2026, 5, 23, 14, 30, tzinfo=timezone.utc)
    start, end = _today_window(now, ZoneInfo("UTC"))
    assert start == datetime(2026, 5, 23, 0, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 5, 24, 0, 0, tzinfo=timezone.utc)


def test_today_window_respects_user_timezone():
    """A user in Tokyo at UTC 14:30 May 23 is in May 23 late-night local;
    'today' brackets their local May 23 (UTC May 22 15:00 → UTC May 23 15:00)."""
    now = datetime(2026, 5, 23, 14, 30, tzinfo=timezone.utc)
    start, end = _today_window(now, ZoneInfo("Asia/Tokyo"))
    assert start == datetime(2026, 5, 22, 15, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 5, 23, 15, 0, tzinfo=timezone.utc)


def test_calendar_month_window_covers_whole_month():
    now = datetime(2026, 5, 23, 14, 30, tzinfo=timezone.utc)
    start, end = _calendar_month_window(now, ZoneInfo("UTC"))
    assert start == datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 6, 1, tzinfo=timezone.utc)


def test_format_period_label_today():
    s = datetime(2026, 5, 23, tzinfo=timezone.utc)
    e = datetime(2026, 5, 24, tzinfo=timezone.utc)
    assert _format_period_label("Today", s, e, ZoneInfo("UTC")) == "Today (May 23)"


def test_format_period_label_range_spans_month():
    s = datetime(2026, 5, 1, tzinfo=timezone.utc)
    e = datetime(2026, 6, 1, tzinfo=timezone.utc)
    label = _format_period_label("Current period", s, e, ZoneInfo("UTC"))
    assert label == "Current period (May 1 – May 31, 2026)"


def test_render_text_empty_state():
    period = PeriodReport(
        label="Today (May 23)",
        start=datetime(2026, 5, 23, tzinfo=timezone.utc),
        end=datetime(2026, 5, 24, tzinfo=timezone.utc),
    )
    report = UsageReport(
        user_id=uuid4(),
        scope="today",
        generated_at=datetime.now(timezone.utc),
        timezone="UTC",
        periods=[period],
    )
    text = render_text(report)
    assert "Today (May 23)" in text
    assert "(no usage yet)" in text


def test_render_text_with_models_and_by_agent():
    period = PeriodReport(
        label="Current period (May 1 – May 31, 2026)",
        start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        end=datetime(2026, 6, 1, tzinfo=timezone.utc),
        models=[
            ModelRow(
                model="claude-haiku-4-5",
                input_tokens=1_243_000,
                output_tokens=340_000,
                cache_read_tokens=0,
                cache_write_tokens=0,
                input_cost_cents=124,
                output_cost_cents=170,
                cache_cost_cents=0,
                total_cost_cents=294,
            ),
            ModelRow(
                model="voyage-3",
                input_tokens=45_000,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                input_cost_cents=2,
                output_cost_cents=0,
                cache_cost_cents=0,
                total_cost_cents=2,
            ),
        ],
        by_agent=[AgentRow(agent="planner", cost_cents=200)],
        total_cost_cents=296,
    )
    report = UsageReport(
        user_id=uuid4(),
        scope="month",
        generated_at=datetime.now(timezone.utc),
        timezone="UTC",
        periods=[period],
        include_by_agent=True,
    )
    text = render_text(report)
    assert "claude-haiku-4-5" in text
    assert "1,243,000 in" in text
    assert "$1.24 + $1.70 = $2.94" in text
    assert "voyage-3" in text
    voyage_line = next(line for line in text.split("\n") if "voyage-3" in line)
    assert "+" not in voyage_line
    assert "$0.02" in voyage_line
    assert "Total: $2.96" in text
    assert "By agent:" in text
    assert "planner" in text
