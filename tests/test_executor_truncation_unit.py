"""Prior-result truncation is counted, not silent (blackboard Phase 0).

`_format_prior_result` clips a step's output before inlining it into the
next step's prompt (and again into the synthesis prompt). That clip is
silent data loss — the downstream call simply never sees the tail. Phase
0 doesn't remove the clip; it makes it countable, so the baseline is
known before Phase 1 removes it.

These tests pin the warning's shape, because the Phase 1 acceptance
criterion is "no `executor.prior_result.truncated` fires on a plan that
logged one here."
"""

from __future__ import annotations

from typing import Any

import pytest

from wolfpaw.agents import executor as executor_mod
from wolfpaw.agents.executor import (
    _PRIOR_RESULT_TRUNCATE,
    _SYNTHESIS_TRUNCATE,
    _build_step_prompt,
    _build_synthesis_prompt,
    _format_prior_result,
)
from wolfpaw.schemas import Plan, Step, StepResult, StepStatus


class RecordingLogger:
    """Stands in for the module logger; keeps every warning emitted."""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kw: Any) -> None:
        self.warnings.append((event, kw))

    def __getattr__(self, _name: str):  # info/debug/exception — ignored
        return lambda *a, **k: None


@pytest.fixture
def rec(monkeypatch) -> RecordingLogger:
    logger = RecordingLogger()
    monkeypatch.setattr(executor_mod, "log", logger)
    return logger


def _result(output: Any, *, step_id: str = "s1", kind: str = "functional") -> StepResult:
    return StepResult(
        step_id=step_id, kind=kind,
        status=StepStatus.COMPLETED, output=output,
    )


def test_short_output_is_not_clipped_and_logs_nothing(rec):
    line = _format_prior_result(_result({"ok": True}), cap=1_000, site="step_prompt")

    assert "truncated" not in line
    assert rec.warnings == []


def test_clip_emits_a_warning_with_the_measured_sizes(rec):
    # 5_000 'x' characters; JSON-encoding adds the surrounding quotes.
    payload = "x" * 5_000
    line = _format_prior_result(_result(payload), cap=1_000, site="step_prompt")

    assert "…(truncated)" in line
    assert len(rec.warnings) == 1

    event, kw = rec.warnings[0]
    assert event == "executor.prior_result.truncated"
    assert kw["site"] == "step_prompt"
    assert kw["step_id"] == "s1"
    assert kw["kind"] == "functional"
    assert kw["cap"] == 1_000
    assert kw["original_bytes"] == 5_002          # 5_000 + two quotes
    assert kw["dropped_bytes"] == 5_002 - 1_000


def test_failed_step_is_summarized_without_touching_the_cap(rec):
    failed = StepResult(
        step_id="s2", kind="functional",
        status=StepStatus.FAILED, error="boom",
    )

    line = _format_prior_result(failed, cap=10, site="synthesis")

    assert line == "- s2 [functional]: (failed)"
    assert rec.warnings == []


def test_step_prompt_reports_the_step_prompt_site(rec):
    plan = Plan(query="q", summary="s", steps=[])
    step = Step(id="s2", kind="reasoning", description="use the prior output")
    prior = [_result("y" * (_PRIOR_RESULT_TRUNCATE + 1_000))]

    _build_step_prompt(step, plan, prior)

    assert [kw["site"] for _, kw in rec.warnings] == ["step_prompt"]
    assert rec.warnings[0][1]["cap"] == _PRIOR_RESULT_TRUNCATE


def test_synthesis_prompt_reports_the_synthesis_site(rec):
    plan = Plan(query="q", summary="s", steps=[])
    results = [_result("z" * (_SYNTHESIS_TRUNCATE + 1_000))]

    _build_synthesis_prompt(plan, results)

    assert [kw["site"] for _, kw in rec.warnings] == ["synthesis"]
    assert rec.warnings[0][1]["cap"] == _SYNTHESIS_TRUNCATE


def test_every_clipped_step_is_counted_separately(rec):
    plan = Plan(query="q", summary="s", steps=[])
    step = Step(id="s9", kind="reasoning", description="synthesize")
    big = "w" * (_PRIOR_RESULT_TRUNCATE + 500)
    prior = [
        _result(big, step_id="s1"),
        _result("small", step_id="s2"),
        _result(big, step_id="s3"),
    ]

    _build_step_prompt(step, plan, prior)

    # Two clips, not one warning for the whole prompt — the point is to
    # learn *which* steps lose data, not just that some prompt did.
    assert [kw["step_id"] for _, kw in rec.warnings] == ["s1", "s3"]
