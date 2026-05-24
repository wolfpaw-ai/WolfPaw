"""Router — composes Triage + downstream agents for every channel.

Channels call `Router.handle(...)`; the router runs Triage to classify the
message, then dispatches to the appropriate handler:
    - "quick" → QuickAgent.handle (final text response)
    - "plan"  → PlannerAgent.plan → ExecutorAgent.execute → final text
    - "task"  → falls through to Quick with a preamble until Tasks (step 15)

The router emits a `triage` event right after classification, then a
`plan` event with the plan summary when the Planner path is taken, then
step-level events from the Executor as the plan runs.
"""

from __future__ import annotations

import json
from typing import Awaitable, Callable
from uuid import UUID

from wolfpaw.agents.executor import ExecutorAgent, get_executor_agent
from wolfpaw.agents.planner import PlannerAgent, get_planner_agent
from wolfpaw.agents.quick import QuickAgent, get_quick_agent
from wolfpaw.agents.triage import (
    Route,
    TriageAgent,
    TriageVerdict,
    get_triage_agent,
)
from wolfpaw.memory import conversational as conv
from wolfpaw.memory.db import acquire
from wolfpaw.schemas import Plan
from wolfpaw.toolbox.registry import ToolContext
from wolfpaw.tracing import get_logger

log = get_logger()

EmitFn = Callable[[str, str], Awaitable[None] | None]

_TASK_FALLBACK_PREAMBLE = (
    "(Triage suggested I track this as a long-running Task, but Tasks"
    " aren't online yet — running through the Quick path instead.)\n\n"
)


class Router:
    def __init__(
        self,
        *,
        triage: TriageAgent | None = None,
        quick: QuickAgent | None = None,
        planner: PlannerAgent | None = None,
        executor: ExecutorAgent | None = None,
    ) -> None:
        self._triage = triage
        self._quick = quick
        self._planner = planner
        self._executor = executor

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
            answer = await self.quick.handle(
                ctx=ctx, thread_id=thread_id, content=content, emit=emit,
            )
            return _TASK_FALLBACK_PREAMBLE + answer

        # Defensive: an unknown route would already have been normalized in
        # TriageAgent.classify, but if it ever escapes, treat as quick.
        log.warning("router.unknown_route", route=verdict.route)
        return await self.quick.handle(
            ctx=ctx, thread_id=thread_id, content=content, emit=emit,
        )

    async def _handle_plan(
        self,
        *,
        ctx: ToolContext,
        thread_id: UUID,
        content: str,
        complexity_hint: str,
        emit: EmitFn | None,
    ) -> str:
        plan, plan_ctx = await self.planner.plan(
            ctx=ctx, thread_id=thread_id, content=content,
            complexity_hint=complexity_hint,
        )
        await _maybe_emit(emit, "plan", _summarize_plan_for_event(plan))

        execution = await self.executor.execute(
            ctx=ctx, plan=plan, emit=emit,
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
