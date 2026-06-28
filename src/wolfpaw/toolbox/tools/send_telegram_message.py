"""`send_telegram_message` — proactively push a Telegram message to the user.

Unlike the inbound reply path (which answers a message the user just sent),
this lets the agent *initiate* an outbound message to the user's own linked
Telegram account — e.g. reminders, scheduled nudges, "your task finished."

Recipient is always the current user (resolved from ``ctx.user_id`` →
``channel_links``); there's no arbitrary-recipient parameter, so the agent
can only message the person it's acting for. Text is run through the same
MarkdownV2 conversion the inbound reply uses so formatting renders.
"""

from __future__ import annotations

from typing import Any

from wolfpaw.memory import channel_links
from wolfpaw.memory.db import acquire
from wolfpaw.toolbox.registry import (
    Tool,
    ToolContext,
    ToolError,
    register_tool,
)


@register_tool
class SendTelegramMessageTool(Tool):
    name = "send_telegram_message"
    description = (
        "Send a Telegram message to the current user. Use this to proactively"
        " reach the user (reminders, notifications, scheduled nudges) rather"
        " than only replying to their messages. Fails if the user hasn't"
        " linked a Telegram account."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Message body. Supports markdown formatting.",
            }
        },
        "required": ["text"],
    }

    async def run(self, ctx: ToolContext, **inputs: Any) -> dict[str, Any]:
        text = inputs.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ToolError("`text` must be a non-empty string")

        from wolfpaw.channels.telegram_client import get_telegram_client
        from wolfpaw.channels.telegram_markdown import to_markdown_v2

        async with acquire() as conn:
            links = await channel_links.list_for_user(conn, user_id=ctx.user_id)
        link = next((l for l in links if l.channel == "telegram"), None)
        if link is None:
            raise ToolError(
                "No linked Telegram account for this user. Link one from the"
                " web app before sending Telegram messages."
            )

        client = get_telegram_client()
        try:
            await client.send_message(
                chat_id=link.external_id,
                text=to_markdown_v2(text),
                parse_mode="MarkdownV2",
            )
        except Exception as e:  # noqa: BLE001 — surface as recoverable tool error
            raise ToolError(f"Telegram send failed: {e}") from e

        return {"sent": True, "chat_id": link.external_id}
