"""Router — composes Triage + downstream agents for every channel.

Channels call `Router.handle(...)`; the router runs Triage to classify the
message, then dispatches to the appropriate handler. Today only the Quick
path exists; "plan" and "task" verdicts fall through to Quick with an
honest preamble explaining the routing the system *would* have done once
the Planner (step 12) and Tasks lifecycle (step 15) are online.

The router emits a `triage` event (via the channel's `emit` callback)
right after classification so the user sees the routing decision before
the answer starts streaming.
"""

from __future__ import annotations

from typing import Awaitable, Callable
from uuid import UUID

from wolfpaw.agents.quick import QuickAgent, get_quick_agent
from wolfpaw.agents.triage import (
    Route,
    TriageAgent,
    TriageVerdict,
    get_triage_agent,
)
from wolfpaw.toolbox.registry import ToolContext
from wolfpaw.tracing import get_logger

log = get_logger()

EmitFn = Callable[[str, str], Awaitable[None] | None]

_PLAN_FALLBACK_PREAMBLE = (
    "(Triage suggested I plan this out, but the Planner isn't online yet"
    " — running through the Quick path instead.)\n\n"
)
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
    ) -> None:
        self._triage = triage
        self._quick = quick

    @property
    def triage(self) -> TriageAgent:
        return self._triage or get_triage_agent()

    @property
    def quick(self) -> QuickAgent:
        return self._quick or get_quick_agent()

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
            answer = await self.quick.handle(
                ctx=ctx, thread_id=thread_id, content=content, emit=emit,
            )
            return _PLAN_FALLBACK_PREAMBLE + answer

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
