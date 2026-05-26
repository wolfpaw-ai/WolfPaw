"""Router — composes Triage + downstream agents for every channel.

Channels call `Router.handle(...)`; the router runs Triage to classify the
message, then dispatches to the appropriate handler:
    - "quick" → QuickAgent.handle (final text response)
    - "plan"  → PlannerAgent.plan → ExecutorAgent.execute
                  → PostEvaluatorAgent.evaluate → final text + score persisted
    - "task"  → TaskService.create_and_run (Task row + same pipeline,
                with ctx.task_id flowing through so `ask_user` works)

SSE events for the plan path, in order:
    triage → plan → (step.start/step.end/step.error per step) →
    score → delta → done

SSE events for the task path add `task` (emitted right after the task
row is created, payload = task_id) so clients can /task <id> for status.
"""

from __future__ import annotations

import json
from typing import Awaitable, Callable
from uuid import UUID

from typing import TYPE_CHECKING

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
from wolfpaw.agents.triage import (
    Route,
    TriageAgent,
    TriageVerdict,
    get_triage_agent,
)
from wolfpaw.memory import conversational as conv
from wolfpaw.memory import procedural, task_events
from wolfpaw.memory.db import acquire
from wolfpaw.schemas import ExecutionPlan, Plan, PostEvalVerdict
from wolfpaw.toolbox.registry import ToolContext
from wolfpaw.tracing import get_logger

# `tasks.service` imports back into `agents.*`, so a top-level import would
# create a cycle. Use a TYPE_CHECKING import for the type hint and a lazy
# import inside the property accessor.
if TYPE_CHECKING:
    from wolfpaw.tasks.service import TaskService

log = get_logger()

EmitFn = Callable[[str, str], Awaitable[None] | None]

_TASK_TITLE_MAX = 80


class Router:
    def __init__(
        self,
        *,
        triage: TriageAgent | None = None,
        quick: QuickAgent | None = None,
        planner: PlannerAgent | None = None,
        executor: ExecutorAgent | None = None,
        pre_evaluator: PlanPreEvaluatorAgent | None = None,
        post_evaluator: PostEvaluatorAgent | None = None,
        task_service: "TaskService | None" = None,
    ) -> None:
        self._triage = triage
        self._quick = quick
        self._planner = planner
        self._executor = executor
        self._pre_evaluator = pre_evaluator
        self._post_evaluator = post_evaluator
        self._task_service = task_service

    @property
    def triage(self) -> TriageAgent:
        return self._triage or get_triage_agent()

    @property
    def quick(self) -> QuickAgent:
        return self._quick or get_quick_agent()

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
    def task_service(self) -> "TaskService":
        if self._task_service is not None:
            return self._task_service
        from wolfpaw.tasks.service import get_task_service

        return get_task_service()

    async def handle(
        self,
        *,
        ctx: ToolContext,
        thread_id: UUID,
        content: str,
        emit: EmitFn | None = None,
    ) -> str:
        verdict = await self.triage.classify(
            ctx=ctx, thread_id=thread_id, content=content,
        )
        log.info(
            "router.triage.verdict",
            user_id=str(ctx.user_id),
            route=verdict.route,
            complexity=verdict.complexity,
        )
        await _maybe_emit(emit, "triage", f"{verdict.route} — {verdict.reasoning}")

        if verdict.route == "quick":
            return await self.quick.handle(
                ctx=ctx, thread_id=thread_id, content=content, emit=emit,
            )

        if verdict.route == "plan":
            return await self._handle_plan(
                ctx=ctx, thread_id=thread_id, content=content,
                complexity_hint=verdict.complexity, emit=emit,
            )

        if verdict.route == "task":
            return await self._handle_task(
                ctx=ctx, thread_id=thread_id, content=content,
                complexity_hint=verdict.complexity, emit=emit,
            )

        # Defensive: an unknown route would already have been normalized in
        # TriageAgent.classify, but if it ever escapes, treat as quick.
        log.warning("router.unknown_route", route=verdict.route)
        return await self.quick.handle(
            ctx=ctx, thread_id=thread_id, content=content, emit=emit,
        )

    async def _handle_task(
        self,
        *,
        ctx: ToolContext,
        thread_id: UUID,
        content: str,
        complexity_hint: str,
        emit: EmitFn | None,
    ) -> str:
        """Long-running work path.

        With ``WOLFPAW_WORKERS_ENABLED=true`` we create the task row,
        enqueue ``run_task`` onto arq, and return an acknowledgement
        immediately — the user follows up via ``/task <id>`` or gets a
        proactive push when the work finishes (step 37 wires that).
        With workers off, the task runs inline so a single-process dev
        gets the synthesized answer in the same response (the v1
        behaviour).
        """
        from wolfpaw.config import get_settings

        settings = get_settings()
        if settings.workers_enabled:
            return await self._handle_task_deferred(
                ctx=ctx, thread_id=thread_id, content=content,
                complexity_hint=complexity_hint, emit=emit,
            )
        return await self._handle_task_inline(
            ctx=ctx, thread_id=thread_id, content=content,
            complexity_hint=complexity_hint, emit=emit,
        )

    async def _handle_task_inline(
        self,
        *,
        ctx: ToolContext,
        thread_id: UUID,
        content: str,
        complexity_hint: str,
        emit: EmitFn | None,
    ) -> str:
        outcome = await self.task_service.create_and_run(
            user_id=ctx.user_id,
            thread_id=thread_id,
            content=content,
            title=_title_from_content(content),
            description=content,
            channel_for_completion="web",
            complexity_hint=complexity_hint,
            emit=emit,
        )
        try:
            async with acquire() as conn:
                await conv.append(
                    conn, thread_id=thread_id, role="user", content=content,
                )
                await conv.append(
                    conn, thread_id=thread_id, role="assistant",
                    content=outcome.final_answer,
                )
        except Exception:  # noqa: BLE001
            log.warning("router.task_persist_failed", exc_info=True)
        return outcome.final_answer

    async def _handle_task_deferred(
        self,
        *,
        ctx: ToolContext,
        thread_id: UUID,
        content: str,
        complexity_hint: str,
        emit: EmitFn | None,
    ) -> str:
        from wolfpaw.workers.queue import enqueue_run_task

        task = await self.task_service.create(
            user_id=ctx.user_id,
            thread_id=thread_id,
            content=content,
            title=_title_from_content(content),
            description=content,
            channel_for_completion="web",
            complexity_hint=complexity_hint,
        )
        await _maybe_emit(emit, "task", str(task.id))
        await enqueue_run_task(task.id)

        ack = (
            f"Started Task {task.id} in the background — "
            f"check `/task {task.id}` for progress, or wait for the push"
            " when it finishes."
        )
        try:
            async with acquire() as conn:
                await conv.append(
                    conn, thread_id=thread_id, role="user", content=content,
                )
                await conv.append(
                    conn, thread_id=thread_id, role="assistant", content=ack,
                )
        except Exception:  # noqa: BLE001
            log.warning("router.task_persist_failed", exc_info=True)
        return ack

    async def _handle_plan(
        self,
        *,
        ctx: ToolContext,
        thread_id: UUID,
        content: str,
        complexity_hint: str,
        emit: EmitFn | None,
    ) -> str:
        plan, _verdict, _retried = await plan_with_pre_evaluation(
            planner=self.planner,
            pre_evaluator=self.pre_evaluator,
            ctx=ctx, thread_id=thread_id, content=content,
            complexity_hint=complexity_hint, emit=emit,
        )
        await _maybe_emit(emit, "plan", _summarize_plan_for_event(plan))

        execution = await self.executor.execute(
            ctx=ctx, plan=plan, emit=emit,
        )

        # Score the run. Scoring is best-effort — never block the user.
        await self._score_and_persist(
            ctx=ctx, plan=plan, execution=execution, emit=emit,
        )

        final = execution.final_answer
        try:
            async with acquire() as conn:
                await conv.append(
                    conn, thread_id=thread_id, role="user", content=content,
                )
                await conv.append(
                    conn, thread_id=thread_id, role="assistant", content=final,
                )
        except Exception:  # noqa: BLE001 — degraded mode
            log.warning("router.plan_persist_failed", exc_info=True)

        return final

    async def _score_and_persist(
        self,
        *,
        ctx: ToolContext,
        plan: Plan,
        execution: ExecutionPlan,
        emit: EmitFn | None,
    ) -> None:
        """Run the Post-Evaluator, persist score to procedural memory,
        write a `plan_scored` task_event, and (step 25) conditionally
        distill a Skill from high-scoring reusable plans. Every step is
        best-effort — scoring + emission must never block returning the
        user's answer."""
        try:
            verdict = await self.post_evaluator.evaluate(
                ctx=ctx, plan=plan, execution=execution,
            )
        except Exception:  # noqa: BLE001
            log.warning("router.post_evaluator_failed", exc_info=True)
            return

        await _maybe_emit(
            emit, "score", f"{verdict.score}/100 — {verdict.summary}",
        )

        # Skill auto-emission (step 25). Self-gated by score threshold +
        # reusability heuristic + dedup; runs to completion or silently
        # returns None.
        try:
            await maybe_distill_skill(
                ctx=ctx, plan=plan, execution=execution,
                verdict=verdict, emit=emit,
            )
        except Exception:  # noqa: BLE001
            log.warning("router.skill_distiller_failed", exc_info=True)

        if plan.id is None:
            # Planner didn't persist — nothing to score in procedural memory.
            return

        try:
            async with acquire() as conn:
                await procedural.update_outcome(
                    conn, plan_id=plan.id, score=verdict.score,
                )
                await task_events.append_event(
                    conn,
                    task_id=ctx.task_id,
                    event_type="plan_scored",
                    content={
                        "plan_id": str(plan.id),
                        **verdict.to_jsonb(),
                    },
                )
        except Exception:  # noqa: BLE001
            log.warning("router.score_persist_failed", exc_info=True)


def _title_from_content(content: str) -> str:
    """First line of the user's message, capped at _TASK_TITLE_MAX chars."""
    first_line = content.strip().split("\n", 1)[0].strip() or "Task"
    if len(first_line) > _TASK_TITLE_MAX:
        first_line = first_line[: _TASK_TITLE_MAX - 1].rstrip() + "…"
    return first_line


def _summarize_plan_for_event(plan: Plan) -> str:
    n = len(plan.steps)
    bits = [f"{n} step{'s' if n != 1 else ''}"]
    if plan.applied_skill_name:
        bits.append(f"skill={plan.applied_skill_name}")
    if plan.adapted_from_past_plan_id:
        bits.append(f"adapted_from={plan.adapted_from_past_plan_id}")
    if plan.is_task:
        bits.append("would create a Task")
    return ", ".join(bits)


async def _maybe_emit(emit: EmitFn | None, event: str, data: str) -> None:
    if emit is None:
        return
    result = emit(event, data)
    if hasattr(result, "__await__"):
        await result


_router: Router | None = None


def get_router() -> Router:
    global _router
    if _router is None:
        _router = Router()
    return _router


def reset_router() -> None:
    """Test/dev hook to drop the cached router."""
    global _router
    _router = None
