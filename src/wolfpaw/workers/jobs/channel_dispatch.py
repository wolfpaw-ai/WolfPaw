"""arq jobs for Telegram + Slack inbound dispatch.

Both channels share the same v1-era pattern: the webhook must ack
within a few seconds, so the actual Router invocation + reply push
happen as a deferred job. Step 23 moves these jobs onto arq so they
survive an app restart.

The jobs call back into the channel module's existing handlers
(``_handle_inbound`` for Telegram, ``_handle_freeform`` for Slack) —
those already know how to format the reply for their channel.
"""

from __future__ import annotations

from uuid import UUID


async def telegram_dispatch_job(
    _ctx: dict, user_id_str: str, tg_chat_id: str, content: str,
) -> None:
    """arq wrapper for :func:`wolfpaw.channels.telegram._handle_inbound`."""
    from wolfpaw.channels.telegram import _handle_inbound

    await _handle_inbound(
        user_id=UUID(user_id_str), tg_chat_id=tg_chat_id, content=content,
    )


async def slack_dispatch_job(
    _ctx: dict,
    user_id_str: str,
    workspace_bot_token: str,
    channel_id: str,
    content: str,
) -> None:
    """arq wrapper for :func:`wolfpaw.channels.slack._handle_freeform`."""
    from wolfpaw.channels.slack import _handle_freeform

    await _handle_freeform(
        user_id=UUID(user_id_str),
        workspace_bot_token=workspace_bot_token,
        channel_id=channel_id, content=content,
    )
