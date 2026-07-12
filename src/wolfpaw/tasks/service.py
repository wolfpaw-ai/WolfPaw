"""TaskService — task lifecycle façade.

Orchestrates the planner → executor → post-eval pipeline behind the task
status machine. Each state transition writes a `task_events` row of
type ``status.<new>``; the DAO's idempotency on terminal states means a
double terminal transition (cancelled mid-flight, retried etc.) is safe.

Three public entry points:

- :meth:`TaskService.create_and_run` — synchronous end-to-end (the v1
  default). Used by subagent execution because the parent's plan
  cannot continue until the child returns.
- :meth:`TaskService.create` — creates the row in ``pending`` and emits
  the ``status.pending`` event, then returns immediately. The Router
  uses this in the v2 flow when workers are enabled, paired with
  :func:`wolfpaw.workers.queue.enqueue_run_task` so the long-running
  pipeline runs on the arq worker rather than blocking the channel.
- :meth:`TaskService.run` — picks up an existing task by id and drives
  it to a terminal state. Invoked by the arq worker's ``run_task_job``;
  also reachable inline via the queue's fallback when workers are off.

The state machine is the source of truth — ``run`` is safe to call
against any task that's still in ``pending`` (it transitions to
``running`` first) and idempotent against tasks that have already
completed/failed/cancelled (the transition helpers no-op on terminal
states).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from uuid import UUID

from wolfpaw.agents.executor import ExecutorAgent, get_executor_agent
from wolfpaw.agents.plan_pre_evaluator import (
    PlanPreEvaluatorAgent,
    get_pre_evaluator_agent,
    plan_with_pre_evaluation,
)
from wolfpaw.agents.planner import PlannerAgent, get_planner_agent
from wolfpaw.agents.post_evaluator import (
    PostEvaluatorAgent,
    get_post_evaluator_agent,
)
from wolfpaw.agents.quick import QuickAgent, get_quick_agent
from wolfpaw.agents.skill_distiller import maybe_distill_skill
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
        pre_evaluator: PlanPreEvaluatorAgent | None = None,
        post_evaluator: PostEvaluatorAgent | None = None,
        quick: QuickAgent | None = None,
        registry: AskUserRegistry | None = None,
    ) -> None:
        self._planner = planner
        self._executor = executor
        self._pre_evaluator = pre_evaluator
        self._post_evaluator = post_evaluator
        self._quick = quick
        self._registry = registry

    @property
    def planner(self) -> PlannerAgent:
        return self._planner or get_planner_agent()

    @property
    def executor(self) -> ExecutorAgent:
        return self._executor or get_executor_agent()

    @property
    def pre_evaluator(self) -> PlanPreEvaluatorAgent:
        return self._pre_evaluator or get_pre_evaluator_agent()

    @property
    def post_evaluator(self) -> PostEvaluatorAgent:
        return self._post_evaluator or get_post_evaluator_agent()

    @property
    def quick(self) -> QuickAgent:
        return self._quick or get_quick_agent()

    @property
    def registry(self) -> AskUserRegistry:
        return self._registry or get_registry()

    # --- public entry points -------------------------------------------------

    async def create(
        self,
        *,
        user_id: UUID,
        thread_id: UUID | None,
        content: str,
        title: str,
        description: str | None = None,
        channel_for_completion: str | None = None,
        complexity_hint: str = "moderate",
        parent_task_id: UUID | None = None,
        budget_cents: int | None = None,
        agentic: bool = False,
    ) -> tasks_dao.Task:
        """Insert the task row in `pending`, stamp a ``status.pending``
        event with the inputs the worker will need (content, thread_id,
        complexity_hint live in the event's `content` JSONB so the run
        side can recover them without a separate side-table).

        ``agentic`` set → the run executes as a tool loop (Quick agent's
        engine) instead of the planner→executor pipeline. Scheduled tasks
        use this so "compose X then send it" works the way it does in chat.

        Returns the new Task; does NOT start the pipeline. Pair with
        :meth:`run` (inline or via the workers queue)."""
        async with acquire() as conn:
            task = await tasks_dao.create(
                conn,
                user_id=user_id,
                title=title,
                description=description,
                channel_for_completion=channel_for_completion,  # type: ignore[arg-type]
                parent_task_id=parent_task_id,
                budget_cents=budget_cents,
            )
            await task_events.append_event(
                conn,
                task_id=task.id,
                event_type="status.pending",
                content={
                    "title": task.title,
                    "content": content,
                    "thread_id": str(thread_id) if thread_id else None,
                    "complexity_hint": complexity_hint,
                    **({"agentic": True} if agentic else {}),
                    **({"parent_task_id": str(parent_task_id)}
                       if parent_task_id else {}),
                },
            )
        return task

    async def run(
        self,
        task_id: UUID,
        *,
        emit: EmitFn | None = None,
    ) -> TaskOutcome:
        """Drive a pending Task to a terminal state.

        Loads the input parameters from the ``status.pending`` event
        (the create() path stamped them there), invokes the planner →
        executor → post-eval pipeline, then transitions to completed /
        failed depending on the executor's outcome.

        Safe to call on tasks already in a terminal state — the
        transition helpers in the DAO are idempotent.

        ``emit`` is honored when run inline (subagent path, dev
        fallback) but generally unused on the arq worker — the worker
        process has no SSE stream to push to.
        """
        # Reload + hydrate the run inputs from the pending-event.
        async with acquire() as conn:
            task = await conn.fetchrow(
                "SELECT id, user_id FROM tasks WHERE id = $1", task_id,
            )
            if task is None:
                raise ValueError(f"Task {task_id} not found")
            user_id: UUID = task["user_id"]

            event_row = await conn.fetchrow(
                "SELECT content FROM task_events"
                " WHERE task_id = $1 AND event_type = 'status.pending'"
                " ORDER BY created_at ASC LIMIT 1",
                task_id,
            )
        if event_row is None:
            raise ValueError(
                f"Task {task_id} has no status.pending event;"
                " cannot recover run inputs"
            )
        # JSONB column normally arrives as a dict (asyncpg codec) but
        # has been seen as a raw JSON string in production — match
        # the defensive pattern used by skills/procedural/tools DAOs.
        raw_content = event_row["content"]
        if isinstance(raw_content, str):
            raw_content = json.loads(raw_content) if raw_content else {}
        ev = dict(raw_content or {})
        content = ev.get("content") or ""
        thread_id_raw = ev.get("thread_id")
        thread_id = UUID(thread_id_raw) if thread_id_raw else None
        complexity_hint = ev.get("complexity_hint") or "moderate"
        agentic = bool(ev.get("agentic"))

        return await self._run_inner(
            user_id=user_id,
            task_id=task_id,
            thread_id=thread_id,
            content=content,
            complexity_hint=complexity_hint,
            emit=emit,
            agentic=agentic,
        )

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
        parent_task_id: UUID | None = None,
        budget_cents: int | None = None,
        precomputed_plan: Plan | None = None,
    ) -> TaskOutcome:
        """Create + immediately run, in the same process. The Router
        uses this when workers are disabled; subagents (step 16) use it
        unconditionally because the parent agent waits for the child's
        output.

        ``parent_task_id`` set → this is a sub-task spawned by a
        parent's subagent step. Budget is informational for now;
        ``spent_cents`` rollup against ``budget_cents`` is a future
        enforcement hook.

        ``precomputed_plan`` set → the task executes the supplied
        Plan directly and skips its internal planning step. The
        Router uses this when its plan-path Planner emitted
        ``is_task=true``, so we don't re-plan inside the task.
        Subagents omit this and re-plan fresh."""
        task = await self.create(
            user_id=user_id,
            thread_id=thread_id,
            content=content,
            title=title,
            description=description,
            channel_for_completion=channel_for_completion,
            complexity_hint=complexity_hint,
            parent_task_id=parent_task_id,
            budget_cents=budget_cents,
        )
        await _maybe_emit(emit, "task", str(task.id))
        return await self._run_inner(
            user_id=user_id,
            task_id=task.id,
            thread_id=thread_id,
            content=content,
            complexity_hint=complexity_hint,
            emit=emit,
            precomputed_plan=precomputed_plan,
        )

    # --- internal pipeline ---------------------------------------------------

    async def _run_inner(
        self,
        *,
        user_id: UUID,
        task_id: UUID,
        thread_id: UUID | None,
        content: str,
        complexity_hint: str,
        emit: EmitFn | None,
        precomputed_plan: Plan | None = None,
        agentic: bool = False,
    ) -> TaskOutcome:
        ctx = ToolContext(user_id=user_id, task_id=task_id, emit=emit)

        # Transition to running.
        await self._transition(
            task_id, "status.running",
            lambda c: tasks_dao.mark_started(c, task_id=task_id),
        )

        # Agentic runs (scheduled tasks) use the Quick agent's tool loop
        # instead of the static planner→executor pipeline — no plan, no
        # procedural-memory scoring.
        if agentic:
            return await self._run_agentic(
                ctx=ctx, user_id=user_id, task_id=task_id,
                content=content, emit=emit,
            )

        # Plan + Pre-Evaluate. Skipped when the Router pre-planned
        # and handed us the Plan (precomputed_plan).
        try:
            if precomputed_plan is not None:
                plan = precomputed_plan
            else:
                plan, _verdict, _retried = await plan_with_pre_evaluation(
                    planner=self.planner,
                    pre_evaluator=self.pre_evaluator,
                    ctx=ctx, thread_id=thread_id,
                    content=content, complexity_hint=complexity_hint,
                    emit=emit,
                )
            if plan.id is not None:
                async with acquire() as conn:
                    await tasks_dao.attach_plan(
                        conn, task_id=task_id, plan_id=plan.id,
                    )
        except Exception as e:  # noqa: BLE001
            log.exception("tasks.service.plan_failed", task_id=str(task_id))
            await self._fail(task_id, f"planner failed: {e}")
            return TaskOutcome(
                task=await self._reload_required(task_id, user_id),
                plan=None, execution=None, verdict=None,
                final_answer=f"I couldn't plan that: {e}",
            )

        # Execute.
        try:
            execution = await self.executor.execute(
                ctx=ctx, plan=plan, emit=emit,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("tasks.service.execute_crashed", task_id=str(task_id))
            await self._fail(task_id, f"executor crashed: {e}")
            return TaskOutcome(
                task=await self._reload_required(task_id, user_id),
                plan=plan, execution=None, verdict=None,
                final_answer=f"The plan failed: {e}",
            )

        # Score (best-effort).
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
                        conn, task_id=task_id, event_type="plan_scored",
                        content={
                            "plan_id": str(plan.id),
                            **verdict.to_jsonb(),
                        },
                    )
        except Exception:  # noqa: BLE001
            log.warning("tasks.service.scoring_failed", exc_info=True)

        # Skill auto-emission (step 25). Self-gated by score + reusability +
        # dedup; never blocks the task's terminal transition.
        if verdict is not None:
            try:
                await maybe_distill_skill(
                    ctx=ctx, plan=plan, execution=execution,
                    verdict=verdict, emit=emit,
                )
            except Exception:  # noqa: BLE001
                log.warning("tasks.service.skill_distiller_failed", exc_info=True)

        # Terminal status.
        if execution.success:
            await self._transition(
                task_id, "status.completed",
                lambda c: tasks_dao.mark_completed(c, task_id=task_id),
            )
        else:
            await self._transition(
                task_id, "status.failed",
                lambda c: tasks_dao.mark_failed(
                    c, task_id=task_id,
                    reason=execution.error or "execution failed",
                ),
                extra_content={"error": execution.error},
            )

        # Authoritative cost roll-up. Done after the terminal transition
        # so the reload below picks it up.
        try:
            async with acquire() as conn:
                await tasks_dao.rollup_spent_cents(conn, task_id=task_id)
        except Exception:  # noqa: BLE001
            log.warning("tasks.service.rollup_failed", task_id=str(task_id),
                        exc_info=True)

        return TaskOutcome(
            task=await self._reload_required(task_id, user_id),
            plan=plan, execution=execution, verdict=verdict,
            final_answer=execution.final_answer,
        )

    async def _run_agentic(
        self,
        *,
        ctx: ToolContext,
        user_id: UUID,
        task_id: UUID,
        content: str,
        emit: EmitFn | None,
    ) -> TaskOutcome:
        """Execute a task as a tool loop (the Quick agent's engine) rather
        than the static planner→executor pipeline. The model calls tools
        iteratively with their real outputs in context, so "compose X then
        send it" works — the exact behavior you get typing in chat. No
        plan, so no post-eval / procedural scoring; delivery happens via
        the tools the loop calls (e.g. send_telegram_message)."""
        try:
            final_answer = await self.quick.run_headless(
                ctx=ctx, content=content, emit=emit,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("tasks.service.agentic_crashed", task_id=str(task_id))
            await self._fail(task_id, f"agentic run crashed: {e}")
            return TaskOutcome(
                task=await self._reload_required(task_id, user_id),
                plan=None, execution=None, verdict=None,
                final_answer=f"The scheduled run failed: {e}",
            )

        # Stamp the final answer onto the completion event — an agentic run
        # has no plan row to inspect, so this is the tasks-tab's only window
        # into what the loop produced. Truncated to keep the event small.
        await self._transition(
            task_id, "status.completed",
            lambda c: tasks_dao.mark_completed(c, task_id=task_id),
            extra_content={"final_answer": final_answer[:2000]},
        )
        try:
            async with acquire() as conn:
                await tasks_dao.rollup_spent_cents(conn, task_id=task_id)
        except Exception:  # noqa: BLE001
            log.warning("tasks.service.rollup_failed", task_id=str(task_id),
                        exc_info=True)

        return TaskOutcome(
            task=await self._reload_required(task_id, user_id),
            plan=None, execution=None, verdict=None,
            final_answer=final_answer,
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
        # Roll up any partial spend (e.g. a planner call that succeeded
        # before the executor crashed). Best-effort.
        try:
            async with acquire() as conn:
                await tasks_dao.rollup_spent_cents(conn, task_id=task_id)
        except Exception:  # noqa: BLE001
            log.warning("tasks.service.rollup_failed", task_id=str(task_id),
                        exc_info=True)

    async def _reload(self, task_id: UUID, user_id: UUID) -> tasks_dao.Task | None:
        async with acquire() as conn:
            return await tasks_dao.get_by_id(conn, user_id=user_id, task_id=task_id)

    async def _reload_required(self, task_id: UUID, user_id: UUID) -> tasks_dao.Task:
        task = await self._reload(task_id, user_id)
        if task is None:
            # The row was deleted between our user_id resolution and
            # this reload — vanishingly unlikely in practice (ON DELETE
            # CASCADE from users wouldn't fire mid-task).
            raise ValueError(f"Task {task_id} disappeared during run")
        return task


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
