"""Two Microsoft Calendar tools (step 32)."""

from __future__ import annotations

from typing import Any

from wolfpaw.integrations.microsoft.client import (
    MicrosoftApiError, MicrosoftClient, MicrosoftNotConnectedError,
)
from wolfpaw.toolbox.registry import (
    Tool, ToolContext, ToolError, register_tool,
)


_NOT_CONNECTED_MSG = (
    "Microsoft Calendar isn't connected for this user. Have them"
    " visit /integrations to connect their Outlook account, then retry."
)


@register_tool
class OutlookListEventsTool(Tool):
    name = "outlook_calendar_list_events"
    description = (
        "List the user's Outlook calendar events in a date-time range."
        " Returns each event's id, subject, start/end times, location,"
        " attendees, and web link. Recurring events are expanded into"
        " instances inside the window."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "start": {
                "type": "string",
                "description": (
                    "ISO-8601 inclusive start of the window."
                    " Example: '2026-05-26T00:00:00'."
                ),
            },
            "end": {
                "type": "string",
                "description": "ISO-8601 exclusive end of the window.",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "default": 20,
            },
        },
        "required": ["start", "end"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        start = inputs.get("start")
        end = inputs.get("end")
        if not isinstance(start, str) or not start.strip():
            raise ToolError("`start` must be a non-empty ISO-8601 string")
        if not isinstance(end, str) or not end.strip():
            raise ToolError("`end` must be a non-empty ISO-8601 string")
        max_results = int(inputs.get("max_results", 20))
        try:
            client = await MicrosoftClient.for_user(ctx.user_id)
        except MicrosoftNotConnectedError as e:
            raise ToolError(_NOT_CONNECTED_MSG) from e
        try:
            events = await client.list_events(
                start=start, end=end, max_results=max_results,
            )
        except MicrosoftApiError as e:
            raise ToolError(f"Outlook list_events failed: {e}") from e
        return {"events": events, "start": start, "end": end}


@register_tool
class OutlookCreateEventTool(Tool):
    name = "outlook_calendar_create_event"
    description = (
        "Create an Outlook calendar event. Times are ISO-8601 strings"
        " interpreted in the supplied `time_zone` (IANA or Windows"
        " name; default UTC). Optionally invites `attendees` by email"
        " and sets a `location` + `body_html` description."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": "Event title.",
            },
            "start": {
                "type": "string",
                "description": "ISO-8601 start, e.g. '2026-05-28T15:00:00'.",
            },
            "end": {
                "type": "string",
                "description": "ISO-8601 end.",
            },
            "time_zone": {
                "type": "string",
                "description": "IANA zone (e.g. 'America/Chicago') or Windows zone. Default UTC.",
                "default": "UTC",
            },
            "body_html": {
                "type": "string",
                "description": "Optional HTML body for the event description.",
            },
            "attendees": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of attendee email addresses.",
            },
            "location": {
                "type": "string",
                "description": "Optional location string.",
            },
        },
        "required": ["subject", "start", "end"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        subject = inputs.get("subject")
        start = inputs.get("start")
        end = inputs.get("end")
        if not isinstance(subject, str) or not subject.strip():
            raise ToolError("`subject` must be a non-empty string")
        if not isinstance(start, str) or not start.strip():
            raise ToolError("`start` must be a non-empty ISO-8601 string")
        if not isinstance(end, str) or not end.strip():
            raise ToolError("`end` must be a non-empty ISO-8601 string")
        time_zone = inputs.get("time_zone") or "UTC"
        body_html = inputs.get("body_html")
        attendees = inputs.get("attendees")
        location = inputs.get("location")
        if attendees is not None and not isinstance(attendees, list):
            raise ToolError("`attendees` must be a list of email strings")
        try:
            client = await MicrosoftClient.for_user(ctx.user_id)
        except MicrosoftNotConnectedError as e:
            raise ToolError(_NOT_CONNECTED_MSG) from e
        try:
            return await client.create_event(
                subject=subject, start=start, end=end,
                body_html=body_html, attendees=attendees,
                location=location, time_zone=time_zone,
            )
        except MicrosoftApiError as e:
            raise ToolError(f"Outlook create_event failed: {e}") from e
