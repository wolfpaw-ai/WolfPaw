"""`cancel_schedule` — stop a scheduled reminder / recurring task.

Marks the schedule cancelled and clears its next run so the dispatcher
never fires it again. Scoped to the calling user, so one user can't cancel
another's schedule.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from wolfpaw.memory import schedules as schedules_dao
from wolfpaw.memory.db import acquire
from wolfpaw.toolbox.registry import (
    Tool,
    ToolContext,
    ToolError,
    register_tool,
)


@register_tool
class CancelScheduleTool(Tool):
    name = "cancel_schedule"
    description = (
        "Cancel a schedule by id so it stops running. Get the id from"
        " list_schedules. Fails if the id is unknown or already finished."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "schedule_id": {
                "type": "string",
                "description": "The schedule's id (UUID).",
            }
        },
        "required": ["schedule_id"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        raw = inputs.get("schedule_id")
        try:
            schedule_id = UUID(str(raw))
        except (ValueError, TypeError) as e:
            raise ToolError(f"invalid schedule_id {raw!r}") from e

        async with acquire() as conn:
            cancelled = await schedules_dao.cancel(
                conn, user_id=ctx.user_id, schedule_id=schedule_id,
            )
        if cancelled is None:
            raise ToolError(
                "No active schedule with that id (it may not exist, belong to"
                " someone else, or already be finished)."
            )
        return {"cancelled": True, "schedule_id": str(cancelled.id)}
