"""`ask_user` — pause a task, ask the user a question, resume on reply.

This is how Wolfpaw's "ask before destructive actions" promise becomes
real instead of aspirational. The Executor invokes `ask_user` mid-plan;
the task transitions to `awaiting_user`, a `user_question` task_event
is recorded, the channel pushes the question to the user, and this
coroutine awaits the answer.

Requires `ctx.task_id` — `ask_user` only works inside a Task (the state
machine needs somewhere to sit). Invoked from a plan-only path (no
task), it returns a `ToolError` so the agent surfaces the misuse rather
than hanging forever.

The reply path is channel-specific:
    - web: POST /channels/web/answer with {question_id, answer}
    - telegram (step 18): next inbound message in the user's thread
      is treated as the answer

Reply submission resolves the asyncio.Future the tool is awaiting; the
task is transitioned back to `running` and the answer flows back to the
Executor as the tool's return value.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from wolfpaw.memory import task_events, tasks as tasks_dao
from wolfpaw.memory.db import acquire
from wolfpaw.tasks.ask_user_registry import get_registry
from wolfpaw.toolbox.registry import (
    Tool,
    ToolContext,
    ToolError,
    register_tool,
)
from wolfpaw.tracing import get_logger

log = get_logger()


@register_tool
class AskUserTool(Tool):
    name = "ask_user"
    requires_task_context = True
    description = (
        "Pause the current task and ask the user a question via their"
        " channel. Returns the user's answer once they reply. Use this"
        " before any destructive or ambiguous action — overwriting"
        " files, sending messages, large purchases, etc."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to ask. Be specific and brief.",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional multiple-choice options the user can pick"
                    " from. Free-form text is always allowed too."
                ),
            },
            "urgency": {
                "type": "string",
                "enum": ["low", "normal", "high"],
                "default": "normal",
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Optional: how long to wait. Default: 5 minutes."
                    " On timeout the task transitions to `blocked` and"
                    " the tool raises."
                ),
                "default": 300,
            },
        },
        "required": ["question"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        if ctx.task_id is None:
            raise ToolError(
                "ask_user requires a Task context. The Triage Agent should"
                " have routed this request to the Task path."
            )

        question = inputs.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ToolError("`question` must be a non-empty string")
        options = inputs.get("options")
        if options is not None and not isinstance(options, list):
            raise ToolError("`options` must be a list of strings")
        urgency = inputs.get("urgency") or "normal"
        timeout = max(1, int(inputs.get("timeout_seconds") or 300))

        registry = get_registry()
        pq = await registry.register(
            user_id=ctx.user_id,
            task_id=ctx.task_id,
            thread_id=None,
            question=question,
            options=options,
            urgency=str(urgency),
        )

        # State transition + question_asked event.
        try:
            async with acquire() as conn:
                await tasks_dao.mark_awaiting_user(
                    conn, task_id=ctx.task_id,
                    reason=f"ask_user: {question[:80]}",
                )
                await task_events.append_event(
                    conn,
                    task_id=ctx.task_id,
                    event_type="user_question",
                    content={
                        "question_id": str(pq.id),
                        "question": question,
                        "options": options or [],
                        "urgency": urgency,
                    },
                )
        except Exception:  # noqa: BLE001
            log.warning("tools.ask_user.persist_failed", exc_info=True)

        # Push the question to the user's live channel. Without this the
        # DB row above is invisible — the client never learns a question
        # is pending, never POSTs an answer, and the await below hangs to
        # timeout. This emit is the outbound leg that makes the pause real.
        if ctx.emit is not None:
            try:
                payload = json.dumps({
                    "question_id": str(pq.id),
                    "question": question,
                    "options": options or [],
                    "urgency": urgency,
                })
                result = ctx.emit("ask_user", payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001 — emit failure shouldn't crash the task
                log.warning("tools.ask_user.emit_failed", exc_info=True)
        else:
            log.warning(
                "tools.ask_user.no_emit",
                task_id=str(ctx.task_id),
                question_id=str(pq.id),
            )

        # Wait for the answer.
        try:
            answer = await asyncio.wait_for(pq.answer_future, timeout=timeout)
        except asyncio.TimeoutError:
            await registry.cancel(pq.id, "timeout")
            try:
                async with acquire() as conn:
                    await tasks_dao.mark_blocked(
                        conn, task_id=ctx.task_id,
                        reason=f"ask_user timed out after {timeout}s",
                    )
                    await task_events.append_event(
                        conn, task_id=ctx.task_id,
                        event_type="user_question_timeout",
                        content={"question_id": str(pq.id)},
                    )
            except Exception:  # noqa: BLE001
                log.warning("tools.ask_user.timeout_persist_failed", exc_info=True)
            raise ToolError(
                f"ask_user timed out after {timeout}s waiting for a reply"
            )

        # Got an answer. Resume the task.
        try:
            async with acquire() as conn:
                await tasks_dao.mark_started(conn, task_id=ctx.task_id)
                await task_events.append_event(
                    conn, task_id=ctx.task_id,
                    event_type="user_answer",
                    content={"question_id": str(pq.id), "answer": answer},
                )
        except Exception:  # noqa: BLE001
            log.warning("tools.ask_user.resume_persist_failed", exc_info=True)

        return {"question_id": str(pq.id), "answer": answer}
