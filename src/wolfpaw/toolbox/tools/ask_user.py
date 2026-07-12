"""`ask_user` — pause a task, ask the user a question, resume on reply.

This is how Wolfpaw's "ask before destructive actions" promise becomes
real instead of aspirational. The Executor invokes `ask_user` mid-plan;
the task transitions to `awaiting_user`, the question is persisted to the
`pending_questions` table (the durable source of truth), delivered to the
user through their channel, and this coroutine waits for the answer.

Requires `ctx.task_id` — `ask_user` only works inside a Task (the state
machine needs somewhere to sit). Invoked from a plan-only path (no
task), it returns a `ToolError` so the agent surfaces the misuse rather
than hanging forever.

Delivery is channel-shaped:
    - live stream present (web SSE, `ctx.emit`): emit an `ask_user` event
      so the client shows a reply box and POSTs /channels/web/answer.
    - no stream (Telegram/Slack, `ctx.emit is None`): push the question
      proactively via the channel's `send()`. The user's next inbound
      message is matched to this pending question and becomes the answer.

Either way the wait is durable: `pending_questions.wait_for_answer` blocks
on a Postgres LISTEN/NOTIFY signal, not an in-process future — so the ask
and the answer can happen in different processes (inline web request vs
arq worker) and still meet at the row.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from wolfpaw.channels import get_channel
from wolfpaw.memory import (
    pending_questions as pq_dao,
    task_events,
    tasks as tasks_dao,
)
from wolfpaw.memory.db import acquire
from wolfpaw.toolbox.registry import (
    Tool,
    ToolContext,
    ToolError,
    register_tool,
)
from wolfpaw.tracing import get_logger

log = get_logger()


def _format_for_push(question: str, options: list[str] | None) -> str:
    """Render the question for a proactive channel push (Telegram/Slack),
    where there's no reply-box UI — options become a numbered list the user
    can answer with free text."""
    if not options:
        return question
    lines = [question, ""]
    for i, opt in enumerate(options, 1):
        lines.append(f"{i}. {opt}")
    lines.append("")
    lines.append("Reply with your choice.")
    return "\n".join(lines)


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

        # Persist the question (source of truth) + mark the task awaiting +
        # log a timeline event, in one connection.
        try:
            async with acquire() as conn:
                pq = await pq_dao.create(
                    conn,
                    task_id=ctx.task_id,
                    user_id=ctx.user_id,
                    thread_id=None,
                    channel=ctx.channel,
                    question=question,
                    options=options,
                    urgency=str(urgency),
                    timeout_seconds=timeout,
                )
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
        except Exception as e:  # noqa: BLE001 — can't proceed without the row
            log.warning("tools.ask_user.persist_failed", exc_info=True)
            raise ToolError(f"couldn't record the question: {e}")

        # Deliver the question to the user. A live stream takes the emit
        # path (web SSE); otherwise push proactively via the channel.
        await self._deliver(ctx, pq.id, question, options, urgency)

        # Wait durably for the answer (LISTEN/NOTIFY, cross-process safe).
        resolved = await pq_dao.wait_for_answer(
            question_id=pq.id, timeout=float(timeout),
        )

        if resolved is None or resolved.status == "pending":
            # Timed out with no answer.
            try:
                async with acquire() as conn:
                    await pq_dao.mark_timeout(conn, question_id=pq.id)
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

        if resolved.status != "answered":
            # Cancelled (task cancelled while awaiting) — surface it.
            raise ToolError(f"ask_user was {resolved.status} before an answer")

        answer = resolved.answer or ""

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

    async def _deliver(
        self,
        ctx: ToolContext,
        question_id: UUID,
        question: str,
        options: list[str] | None,
        urgency: str,
    ) -> None:
        """Surface the question to the user. Emit into a live stream if one
        exists; else push proactively through the originating channel. Best
        effort — a delivery failure leaves the row pending and the wait will
        time out rather than crash the task."""
        if ctx.emit is not None:
            try:
                payload = json.dumps({
                    "question_id": str(question_id),
                    "question": question,
                    "options": options or [],
                    "urgency": urgency,
                })
                result = ctx.emit("ask_user", payload)
                if hasattr(result, "__await__"):
                    await result
                return
            except Exception:  # noqa: BLE001
                log.warning("tools.ask_user.emit_failed", exc_info=True)
                return

        channel = get_channel(ctx.channel) if ctx.channel else None
        if channel is None:
            log.warning(
                "tools.ask_user.no_delivery_channel",
                task_id=str(ctx.task_id),
                question_id=str(question_id),
                channel=ctx.channel,
            )
            return
        try:
            await channel.send(
                ctx.user_id, _format_for_push(question, options),
            )
        except NotImplementedError:
            # e.g. web with no live stream — no push transport yet.
            log.warning(
                "tools.ask_user.channel_no_push",
                channel=ctx.channel, question_id=str(question_id),
            )
        except Exception:  # noqa: BLE001
            log.warning("tools.ask_user.channel_send_failed", exc_info=True)
