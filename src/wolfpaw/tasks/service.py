"""TaskService — task lifecycle façade.

Orchestrates the synchronous task path: create a task, plan, execute,
score, transition state, emit task_events. Wraps the existing Planner →
Executor → Post-Evaluator chain so the Router can call `create_and_run`
once on a task verdict.

State transitions are the source of truth — every transition writes a
`task_events` row of type `status.<new>` with the prior status carried
in `content`. The DAO's idempotency on terminal states means a double
`mark_completed` is safe.

When the arq worker lands (follow-up), it'll call `run(task_id)` on a
task already in `pending` rather than `create_and_run`. The same
sequence applies; the worker just picks up the queued task instead of
the Router executing it inline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from uuid import UUID

from wolfpaw.agents.executor import ExecutorAgent, get_executor_agent
from wolfpaw.agents.planner import PlannerAgent, get_planner_agent
from wolfpaw.agents.post_evaluator import (
    PostEvaluatorAgent,
    get_post_evaluator_agent,
)
from wolfpaw.memory import procedural, task_events, tasks as tasks_dao
from wolfpaw.memory.db import acquire
from wolfpaw.schemas import ExecutionPlan, Plan, PostEvalVerdict
from wolfpaw.tasks.ask_user_registry import AskUserRegistry, get_registry
from wolfpaw.toolbox.registry import ToolContext
from wolfpaw.tracing import get_logger

log = get_logger()

EmitFn = Callable[[str, str], Awaitable[None] | None]


@dataclass(frozen=True)
class TaskOutcome:
    task: tasks_dao.Task
    plan: Plan | None
    execution: ExecutionPlan | None
    verdict: PostEvalVerdict | None
    final_answer: str


class TaskService:
    def __init__(
        self,
        *,
        planner: PlannerAgent | None = None,
        executor: ExecutorAgent | None = None,
        post_evaluator: PostEvaluatorAgent | None = None,
        registry: AskUserRegistry | None = None,
    ) -> None:
        self._planner = planner
        self._executor = executor
        self._post_evaluator = post_evaluator
        self._registry = registry

    @property
    def planner(self) -> PlannerAgent:
        return self._planner or get_planner_agent()

    @property
    def executor(self) -> ExecutorAgent:
        return self._executor or get_executor_agent()

    @property
    def post_evaluator(self) -> PostEvaluatorAgent:
        return self._post_evaluator or get_post_evaluator_agent()

    @property
    def registry(self) -> AskUserRegistry:
        return self._registry or get_registry()

    async def create_and_run(
        self,
        *,
        user_id: UUID,
        thread_id: UUID | None,
        content: str,
        title: str,
        description: str | None = None,
        channel_for_completion: str | None = None,
        complexity_hint: str = "moderate",
        emit: EmitFn | None = None,
    ) -> TaskOutcome:
        """Create a Task, plan + execute + score, transition through
        states. Runs synchronously in the calling process."""
        # 1. Create the task row in `pending`.
        async with acquire() as conn:
            task = await tasks_dao.create(
                conn,
                user_id=user_id,
                title=title,
                description=description,
                channel_for_completion=channel_for_completion,  # type: ignore[arg-type]
            )
            await task_events.append_event(
                conn,
                task_id=task.id,
                event_type="status.pending",
                content={"title": task.title},
            )
        await _maybe_emit(emit, "task", str(task.id))
        ctx = ToolContext(user_id=user_id, task_id=task.id)

        # 2. Transition to running.
        await self._transition(task.id, "status.running", lambda c:
                               tasks_dao.mark_started(c, task_id=task.id))

        # 3. Plan.
        try:
            plan, _plan_ctx = await self.planner.plan(
                ctx=ctx, thread_id=thread_id or task.id,
                content=content, complexity_hint=complexity_hint,
            )
            if plan.id is not None:
                async with acquire() as conn:
                    await tasks_dao.attach_plan(
                        conn, task_id=task.id, plan_id=plan.id,
                    )
        except Exception as e:  # noqa: BLE001
            log.exception("tasks.service.plan_failed", task_id=str(task.id))
            await self._fail(task.id, f"planner failed: {e}")
            return TaskOutcome(
                task=await self._reload(task.id, user_id) or task,
                plan=None, execution=None, verdict=None,
                final_answer=f"I couldn't plan that: {e}",
            )

        # 4. Execute.
        try:
            execution = await self.executor.execute(
                ctx=ctx, plan=plan, emit=emit,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("tasks.service.execute_crashed", task_id=str(task.id))
            await self._fail(task.id, f"executor crashed: {e}")
            return TaskOutcome(
                task=await self._reload(task.id, user_id) or task,
                plan=plan, execution=None, verdict=None,
                final_answer=f"The plan failed: {e}",
            )

        # 5. Score (best-effort).
        verdict: PostEvalVerdict | None = None
        try:
            verdict = await self.post_evaluator.evaluate(
                ctx=ctx, plan=plan, execution=execution,
            )
            await _maybe_emit(
                emit, "score", f"{verdict.score}/100 — {verdict.summary}",
            )
            if plan.id is not None:
                async with acquire() as conn:
                    await procedural.update_outcome(
                        conn, plan_id=plan.id, score=verdict.score,
                    )
                    await task_events.append_event(
                        conn, task_id=task.id, event_type="plan_scored",
                        content={
                            "plan_id": str(plan.id),
                            **verdict.to_jsonb(),
                        },
                    )
        except Exception:  # noqa: BLE001
            log.warning("tasks.service.scoring_failed", exc_info=True)

        # 6. Terminal status — completed if execution.success else failed.
        if execution.success:
            await self._transition(
                task.id, "status.completed",
                lambda c: tasks_dao.mark_completed(c, task_id=task.id),
            )
        else:
            await self._transition(
                task.id, "status.failed",
                lambda c: tasks_dao.mark_failed(
                    c, task_id=task.id,
                    reason=execution.error or "execution failed",
                ),
                extra_content={"error": execution.error},
            )

        return TaskOutcome(
            task=await self._reload(task.id, user_id) or task,
            plan=plan, execution=execution, verdict=verdict,
            final_answer=execution.final_answer,
        )

    # --- transitions ------------------------------------------------------

    async def _transition(
        self,
        task_id: UUID,
        event_type: str,
        op: Callable[[Any], Awaitable[tasks_dao.Task | None]],
        extra_content: dict[str, Any] | None = None,
    ) -> None:
        try:
            async with acquire() as conn:
                updated = await op(conn)
                if updated is None:
                    # Task was already terminal (cancelled mid-flight, etc).
                    return
                await task_events.append_event(
                    conn, task_id=task_id, event_type=event_type,
                    content=(extra_content or {}) | {"status": updated.status},
                )
        except Exception:  # noqa: BLE001 — never crash the agent on a transition write
            log.warning(
                "tasks.service.transition_failed",
                task_id=str(task_id), event_type=event_type, exc_info=True,
            )

    async def _fail(self, task_id: UUID, reason: str) -> None:
        await self._transition(
            task_id, "status.failed",
            lambda c: tasks_dao.mark_failed(c, task_id=task_id, reason=reason),
            extra_content={"reason": reason},
        )

    async def _reload(self, task_id: UUID, user_id: UUID) -> tasks_dao.Task | None:
        async with acquire() as conn:
            return await tasks_dao.get_by_id(conn, user_id=user_id, task_id=task_id)


async def _maybe_emit(emit: EmitFn | None, event: str, data: str) -> None:
    if emit is None:
        return
    result = emit(event, data)
    if hasattr(result, "__await__"):
        await result


_service: TaskService | None = None


def get_task_service() -> TaskService:
    global _service
    if _service is None:
        _service = TaskService()
    return _service


def reset_task_service() -> None:
    global _service
    _service = None
