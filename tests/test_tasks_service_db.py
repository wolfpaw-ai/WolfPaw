"""DB-backed tests for TaskService — the step-23 create/run split.

The interesting invariant: ``TaskService.run(task_id)`` recovers the
input parameters (content, thread_id, complexity_hint) from the
``status.pending`` event that ``create()`` stamps. Without that
recovery the arq worker has nothing to plan with.

Skipped without WOLFPAW_TEST_DATABASE_URL."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest

from wolfpaw.memory import tasks as tasks_dao
from wolfpaw.memory.db import apply_sql_file, close_pool, migrations_dir
from wolfpaw.schemas import (
    ExecutionPlan, Plan, PostEvalVerdict, PreEvalVerdict,
    Step, StepResult, StepStatus,
)
from wolfpaw.tasks.service import TaskService

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
            "008_conv_compaction.sql",
        ):
            await apply_sql_file(conn, migrations_dir() / f)
    finally:
        await conn.close()
    await close_pool()
    yield
    await close_pool()


async def _seed_user(dsn: str) -> UUID:
    conn = await asyncpg.connect(dsn=dsn)
    try:
        return await conn.fetchval(
            "INSERT INTO users (email, email_verified) VALUES ($1, TRUE)"
            " RETURNING id",
            f"svc+{uuid4().hex[:8]}@test.local",
        )
    finally:
        await conn.close()


# --- fakes -----------------------------------------------------------------


@dataclass
class FakePlanner:
    calls: list[dict] = field(default_factory=list)

    async def plan(self, *, ctx, thread_id, content, complexity_hint,
                   revision_diagnosis=None):
        self.calls.append({
            "user_id": ctx.user_id, "task_id": ctx.task_id,
            "thread_id": thread_id, "content": content,
            "complexity_hint": complexity_hint,
            "revision_diagnosis": revision_diagnosis,
        })
        from wolfpaw.agents.planner import PlanContext

        return (
            Plan(
                query=content, summary="fake plan", is_task=True,
                model_used="claude-sonnet-4-6",
                steps=[Step(id="s1", kind="reasoning", description="t")],
            ),
            PlanContext(
                past_plans=[], relevant_skills=[],
                summaries=[], vector_recall=[],
            ),
        )


@dataclass
class FakePreEvaluator:
    """Approves every first-pass draft."""

    calls: list[dict] = field(default_factory=list)

    async def evaluate(self, *, ctx, content, plan, past_plans=None):
        self.calls.append({"plan_summary": plan.summary})
        return PreEvalVerdict(
            approved=True, achieves_objective=True,
            simplifiable=False, better_than_past_plans=True,
            diagnosis="(test default)",
        )


@dataclass
class FakeExecutor:
    success: bool = True
    final_answer: str = "done."

    async def execute(self, *, ctx, plan, emit=None):
        return ExecutionPlan(
            plan=plan,
            results=[
                StepResult(
                    step_id="s1", status=StepStatus.COMPLETED,
                    output="t", error=None,
                )
            ],
            final_answer=self.final_answer,
            success=self.success,
            error=None,
        )


@dataclass
class FakePostEvaluator:
    async def evaluate(self, *, ctx, plan, execution):
        return PostEvalVerdict(
            score=90, summary="great",
            what_went_well=["ok"], what_went_wrong=[], improvements=[],
        )


def _service() -> TaskService:
    return TaskService(
        planner=FakePlanner(),  # type: ignore[arg-type]
        executor=FakeExecutor(),  # type: ignore[arg-type]
        pre_evaluator=FakePreEvaluator(),  # type: ignore[arg-type]
        post_evaluator=FakePostEvaluator(),  # type: ignore[arg-type]
    )


# --- tests -----------------------------------------------------------------


async def test_create_returns_pending_task_without_running_pipeline():
    """create() inserts the row in `pending` and stamps a
    `status.pending` event with the run inputs. It does NOT call the
    planner."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)

    planner = FakePlanner()
    svc = TaskService(
        planner=planner,  # type: ignore[arg-type]
        executor=FakeExecutor(),  # type: ignore[arg-type]
        pre_evaluator=FakePreEvaluator(),  # type: ignore[arg-type]
        post_evaluator=FakePostEvaluator(),  # type: ignore[arg-type]
    )

    task = await svc.create(
        user_id=uid, thread_id=None,
        content="research X long-term",
        title="research X",
        complexity_hint="ambitious",
        channel_for_completion="web",
    )
    assert task.status == "pending"
    assert planner.calls == [], "create() must not invoke the planner"

    # The pending event carries the inputs run() needs to recover.
    conn = await asyncpg.connect(dsn=dsn)
    try:
        ev = await conn.fetchrow(
            "SELECT content FROM task_events"
            " WHERE task_id = $1 AND event_type = 'status.pending'"
            " ORDER BY created_at ASC LIMIT 1",
            task.id,
        )
    finally:
        await conn.close()
    payload = ev["content"]
    assert payload["content"] == "research X long-term"
    assert payload["complexity_hint"] == "ambitious"
    assert payload["thread_id"] is None


async def test_run_recovers_inputs_from_pending_event_and_drives_pipeline():
    """run(task_id) hydrates content + thread_id + complexity_hint
    from the status.pending event and runs the planner against them."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)

    planner = FakePlanner()
    svc = TaskService(
        planner=planner,  # type: ignore[arg-type]
        executor=FakeExecutor(success=True, final_answer="all good"),  # type: ignore[arg-type]
        pre_evaluator=FakePreEvaluator(),  # type: ignore[arg-type]
        post_evaluator=FakePostEvaluator(),  # type: ignore[arg-type]
    )
    task = await svc.create(
        user_id=uid, thread_id=None, content="do the thing",
        title="thing", complexity_hint="moderate",
    )

    outcome = await svc.run(task.id)
    assert outcome.final_answer == "all good"
    assert outcome.task.status == "completed"
    assert planner.calls == [
        {"user_id": uid, "task_id": task.id, "thread_id": None,
         "content": "do the thing", "complexity_hint": "moderate",
         "revision_diagnosis": None}
    ]


async def test_run_raises_on_unknown_task():
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    await _seed_user(dsn)
    svc = _service()
    with pytest.raises(ValueError, match="not found"):
        await svc.run(uuid4())


async def test_create_and_run_remains_synchronous_for_subagent_path():
    """create_and_run still works end-to-end (subagent path relies on it)."""
    dsn = os.environ["WOLFPAW_TEST_DATABASE_URL"]
    uid = await _seed_user(dsn)
    svc = _service()
    outcome = await svc.create_and_run(
        user_id=uid, thread_id=None, content="inline run",
        title="inline", complexity_hint="moderate",
    )
    assert outcome.final_answer == "done."
    assert outcome.task.status == "completed"
