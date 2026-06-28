"""`update_schedule_state` — let a scheduled run remember things for next time.

A schedule's ``state`` JSONB is scratch the agent can carry across runs —
e.g. "I already warned the user about this rain front," so a 10-minute
recurring check doesn't re-notify every fire. The dispatcher injects the
current state into each run's prompt; this tool writes the (replaced) state
back. The ``schedule_id`` is given to the agent in the run's prompt.

Scoped to the calling user, so a run can only touch its own user's
schedules.
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
class UpdateScheduleStateTool(Tool):
    name = "update_schedule_state"
    description = (
        "Save state for a schedule so future runs don't repeat work (e.g."
        " record that you already notified the user). Replaces the schedule's"
        " stored state with the object you pass. Use the schedule_id given in"
        " your run prompt. Only valid inside a scheduled run."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "schedule_id": {
                "type": "string",
                "description": "The schedule's id (from your run prompt).",
            },
            "state": {
                "type": "object",
                "description": "The full new state object to store.",
            },
        },
        "required": ["schedule_id", "state"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        raw = inputs.get("schedule_id")
        try:
            schedule_id = UUID(str(raw))
        except (ValueError, TypeError) as e:
            raise ToolError(f"invalid schedule_id {raw!r}") from e

        state = inputs.get("state")
        if not isinstance(state, dict):
            raise ToolError("`state` must be an object")

        async with acquire() as conn:
            ok = await schedules_dao.update_state(
                conn, user_id=ctx.user_id, schedule_id=schedule_id, state=state,
            )
        if not ok:
            raise ToolError(
                "No schedule with that id for this user; state not saved."
            )
        return {"updated": True, "schedule_id": str(schedule_id)}
