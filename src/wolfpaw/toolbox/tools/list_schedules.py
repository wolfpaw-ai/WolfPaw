"""`list_schedules` — show the user's active/paused schedules.

Lets the agent answer "what reminders do I have?" and gives it the
``schedule_id`` values needed to cancel one. Terminal (done/cancelled)
schedules are omitted by default.
"""

from __future__ import annotations

from typing import Any

from wolfpaw.memory import schedules as schedules_dao
from wolfpaw.memory.db import acquire
from wolfpaw.toolbox.registry import Tool, ToolContext, register_tool


@register_tool
class ListSchedulesTool(Tool):
    name = "list_schedules"
    description = (
        "List the user's active and paused schedules (reminders / recurring"
        " tasks), soonest first. Returns each schedule's id, title,"
        " instruction, recurrence, and next run time."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "include_terminal": {
                "type": "boolean",
                "description": "Also include done/cancelled schedules.",
            }
        },
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        include_terminal = bool(inputs.get("include_terminal"))
        async with acquire() as conn:
            rows = await schedules_dao.list_for_user(
                conn, user_id=ctx.user_id, include_terminal=include_terminal,
            )
        return {
            "schedules": [
                {
                    "schedule_id": str(s.id),
                    "title": s.title,
                    "instruction": s.instruction,
                    "recurrence": s.recurrence,
                    "status": s.status,
                    "next_run_at": s.next_run_at.isoformat()
                    if s.next_run_at else None,
                    "run_count": s.run_count,
                }
                for s in rows
            ]
        }
