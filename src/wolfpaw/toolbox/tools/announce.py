"""`announce` — say something aloud on the user's voice device.

The voice counterpart of `send_telegram_message`: lets the agent *initiate*
speech, for example a reminder from a scheduled run ("Your laundry is done")
or an update partway through a long task. The text is spoken by the device,
so it should be plain, short sentences.

Goes to the device the user spoke to most recently, or a named one. If no
paired device can speak right now, the message falls back to Telegram so it
isn't lost; the result says which happened.

`set_timer` starts a countdown that runs on the device itself, so it stays
exact even if the network drops and survives the device rebooting.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from wolfpaw.channels import get_channel
from wolfpaw.channels.voice_hub import get_bus, pick_device
from wolfpaw.toolbox.registry import Tool, ToolContext, ToolError, register_tool


def _voice_channel():
    """The voice channel, imported lazily: it pulls in the agent router, which
    imports this toolbox, so a top-level import would be circular."""
    channel = get_channel("voice")
    if channel is None:
        raise ToolError("The voice channel isn't available.")
    return channel


async def _can_speak(ctx: ToolContext) -> bool:
    devices = await get_bus().devices_for_user(str(ctx.user_id))
    return pick_device(devices) is not None


@register_tool
class AnnounceTool(Tool):
    name = "announce"
    description = (
        "Say something aloud on the user's voice device (a smart speaker or"
        " headset), e.g. a reminder or a status update. Keep it to one or two"
        " plain spoken sentences with no markdown or links. Use priority"
        " 'alarm' only for things that must not be missed (a wake-up alarm,"
        " something urgent): the device repeats it until the user stops it."
        " If no voice device is available the message goes to Telegram"
        " instead."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "What to say."},
            "priority": {
                "type": "string",
                "enum": ["info", "alarm"],
                "default": "info",
            },
            "expires_in_seconds": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Drop it if it can't be spoken within this many seconds"
                    " (useful for time-sensitive things)."
                ),
            },
        },
        "required": ["text"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        text = inputs.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ToolError("`text` must be a non-empty string")
        priority = inputs.get("priority") or "info"
        if priority not in ("info", "alarm"):
            raise ToolError("`priority` must be 'info' or 'alarm'")

        expires_at = None
        ttl = inputs.get("expires_in_seconds")
        if ttl is not None:
            if not isinstance(ttl, int) or ttl <= 0:
                raise ToolError("`expires_in_seconds` must be a positive integer")
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)

        on_device = await _can_speak(ctx)
        await _voice_channel().send(
            ctx.user_id, text.strip(),
            priority=priority, expires_at=expires_at, message_id=str(uuid4()),
        )
        return {"delivered_to": "voice device" if on_device else "telegram (no voice device available)"}


@register_tool
class SetTimerTool(Tool):
    name = "set_timer"
    description = (
        "Start a countdown on the user's voice device. When it ends the device"
        " says the label aloud. Use this for short timers ('set a timer for"
        " ten minutes for the pasta'); it runs on the device, so it stays"
        " accurate even if the network drops. For reminders later in the day"
        " or on a schedule, use schedule_task instead."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
            "label": {
                "type": "string",
                "description": "What the device says when the time is up, e.g. 'The pasta is ready.'",
            },
        },
        "required": ["seconds", "label"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        seconds = inputs.get("seconds")
        label = inputs.get("label")
        if not isinstance(seconds, int) or not 1 <= seconds <= 86400:
            raise ToolError("`seconds` must be an integer between 1 and 86400")
        if not isinstance(label, str) or not label.strip():
            raise ToolError("`label` must be a non-empty string")

        started = await _voice_channel().send_timer(ctx.user_id, seconds=seconds, label=label.strip())
        if not started:
            raise ToolError(
                "No voice device is connected to run the timer. Offer to set a"
                " reminder with schedule_task instead."
            )
        return {"started": True, "seconds": seconds, "label": label.strip()}
