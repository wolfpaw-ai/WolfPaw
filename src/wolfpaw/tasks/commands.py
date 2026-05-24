"""Slash commands for tasks: `/tasks`, `/task <id>`, `/cancel <id>`.

Registered with the channel dispatcher at module import (same pattern
as `/usage`). Render plaintext for the SSE channel; future channels
(Telegram) can override via the dispatcher's per-channel renderer
(landing with step 18)."""

from __future__ import annotations

from uuid import UUID

from wolfpaw.channels import InboundMessage
from wolfpaw.channels.commands import CommandResult, register
from wolfpaw.memory import task_events, tasks as tasks_dao
from wolfpaw.memory.db import acquire
from wolfpaw.tracing import get_logger

log = get_logger()


def _parse_uuid(s: str) -> UUID | None:
    try:
        return UUID(s.strip())
    except (TypeError, ValueError):
        return None


@register("tasks", "Show your recent tasks (active + completed).")
async def _tasks_command(message: InboundMessage, args: str) -> CommandResult:
    try:
        async with acquire() as conn:
            rows = await tasks_dao.list_for_user(
                conn, user_id=message.user_id, limit=20,
            )
    except Exception as e:  # noqa: BLE001
        log.warning("tasks.commands.list_failed", exc_info=True)
        return CommandResult(text=f"Couldn't list tasks: {e}")
    if not rows:
        return CommandResult(text="No tasks yet.")
    lines = ["Recent tasks:"]
    for t in rows:
        ago = _format_when(t.last_active_at or t.created_at)
        blocker = f" — blocked: {t.blocking_reason}" if t.blocking_reason else ""
        lines.append(
            f"  [{t.status:<14}] {t.id}  {t.title}  ({ago}){blocker}"
        )
    return CommandResult(text="\n".join(lines))


@register("task", "Show details for one task: `/task <id>`.")
async def _task_command(message: InboundMessage, args: str) -> CommandResult:
    task_id = _parse_uuid(args)
    if task_id is None:
        return CommandResult(
            text="Usage: /task <id>. Use /tasks to find an id.",
        )
    try:
        async with acquire() as conn:
            task = await tasks_dao.get_by_id(
                conn, user_id=message.user_id, task_id=task_id,
            )
            events = (
                await task_events.fetch_for_task(conn, task_id=task_id, limit=50)
                if task is not None else []
            )
    except Exception as e:  # noqa: BLE001
        log.warning("tasks.commands.detail_failed", exc_info=True)
        return CommandResult(text=f"Couldn't load task: {e}")
    if task is None:
        return CommandResult(text=f"No task with id {task_id} (for you).")
    lines = [
        f"Task {task.id}",
        f"  Title:  {task.title}",
        f"  Status: {task.status}",
        f"  Spent:  {task.spent_cents}¢",
    ]
    if task.blocking_reason:
        lines.append(f"  Blocking: {task.blocking_reason}")
    if task.description:
        lines.append(f"  Description: {task.description}")
    if events:
        lines.append("  Events:")
        for e in events[-15:]:  # tail
            when = e.created_at.strftime("%H:%M:%S") if e.created_at else "?"
            lines.append(f"    {when}  {e.event_type}")
    return CommandResult(text="\n".join(lines))


@register("cancel", "Cancel a running task: `/cancel <id>`.")
async def _cancel_command(message: InboundMessage, args: str) -> CommandResult:
    task_id = _parse_uuid(args)
    if task_id is None:
        return CommandResult(text="Usage: /cancel <id>.")
    try:
        async with acquire() as conn:
            cancelled = await tasks_dao.cancel(
                conn, user_id=message.user_id, task_id=task_id,
            )
            if cancelled is not None:
                await task_events.append_event(
                    conn, task_id=task_id, event_type="status.cancelled",
                    content={"by": "user"},
                )
    except Exception as e:  # noqa: BLE001
        log.warning("tasks.commands.cancel_failed", exc_info=True)
        return CommandResult(text=f"Couldn't cancel: {e}")
    if cancelled is None:
        return CommandResult(
            text=(
                f"Task {task_id} couldn't be cancelled — it's either"
                " already terminal or doesn't belong to you."
            ),
        )
    return CommandResult(text=f"Cancelled task {task_id}.")


def _format_when(dt) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M")
