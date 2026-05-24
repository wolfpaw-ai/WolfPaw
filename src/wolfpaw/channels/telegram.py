"""Telegram channel — webhook + deep-link onboarding + outbound `send`.

Routes:
    POST  /channels/telegram/webhook            — Telegram → us
    POST  /channels/telegram/link-token         — web client → us (mint)

Deep-link onboarding flow:
    1. Authenticated Wolfpaw user POSTs to /link-token
    2. We mint a single-use token (15 min TTL) bound to the user_id
    3. Response: {"url": "https://t.me/<bot>?start=link_<token>"}
    4. User opens the link in Telegram, taps "Start"
    5. Telegram sends `/start link_<token>` to our webhook
    6. We validate the token, write the channel_links row, send a
       confirmation message back via sendMessage

Inbound message flow (linked user):
    1. Telegram POSTs an update to /webhook
    2. Validate `X-Telegram-Bot-Api-Secret-Token` matches our config
    3. Look up the user via channel_links by the Telegram user_id
    4. Dispatch slash commands through the shared dispatcher (no model call)
    5. For free-form text: fire-and-forget asyncio.create_task that runs
       the Router and sends the final answer back via the Telegram client
    6. Return 200 OK to Telegram immediately (their timeout is short)

The fire-and-forget pattern is the same v1 trade the web channel makes:
if the process dies before the background task completes, the user
never sees a reply. arq (deferred) would move the background work to a
proper queue.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from starlette.responses import Response

from wolfpaw.agents.router import get_router
from wolfpaw.auth.deps import require_user_id
from wolfpaw.channels import Channel, InboundMessage
from wolfpaw.channels.commands import get_dispatcher
from wolfpaw.channels.telegram_client import TelegramClient, get_telegram_client
from wolfpaw.channels.telegram_tokens import (
    LinkTokenError,
    consume as consume_link_token,
    issue as issue_link_token,
)
from wolfpaw.config import get_settings
from wolfpaw.memory import channel_links, conversational as conv
from wolfpaw.memory.db import acquire
from wolfpaw.toolbox.registry import ToolContext
from wolfpaw.tracing import get_logger

log = get_logger()
router = APIRouter(prefix="/channels/telegram", tags=["channels"])

_LINK_PREFIX = "/start link_"


class TelegramChannel(Channel):
    name = "telegram"

    async def receive(self, payload: dict) -> InboundMessage:
        """Parse a Telegram `message` object (already extracted from the
        outer Update) into our normalized shape. Callers pass `user_id`
        in the payload after resolving via channel_links."""
        return InboundMessage(
            user_id=payload["user_id"],
            content=payload["content"],
            channel_name=self.name,
            thread_id=payload.get("thread_id"),
            metadata=payload.get("metadata", {}),
        )

    async def send(self, user_id: UUID, content: str, **kwargs: Any) -> None:
        """Push a message to the user via the Bot API. Looks up the
        user's Telegram chat_id from channel_links — used by proactive
        notifications (task completed, awaiting_user) once those wire in."""
        async with acquire() as conn:
            link = await _user_telegram_link(conn, user_id)
        if link is None:
            log.warning(
                "channels.telegram.send.not_linked",
                user_id=str(user_id),
            )
            return
        client = get_telegram_client()
        await client.send_message(chat_id=link.external_id, text=content)

    def supports_streaming(self) -> bool:
        return False  # Telegram is push, not streaming


_channel = TelegramChannel()


async def _user_telegram_link(conn, user_id: UUID):
    links = await channel_links.list_for_user(conn, user_id=user_id)
    for link in links:
        if link.channel == "telegram":
            return link
    return None


# --- /link-token endpoint (web client → us) -------------------------------


class LinkTokenResponse(BaseModel):
    url: str
    expires_in_minutes: int


@router.post("/link-token", response_model=LinkTokenResponse)
async def mint_link_token(
    user_id: UUID = Depends(require_user_id),
) -> LinkTokenResponse:
    settings = get_settings()
    if not settings.telegram_bot_username:
        raise HTTPException(503, "Telegram bot username not configured")
    async with acquire() as conn:
        plain = await issue_link_token(
            conn,
            user_id=user_id,
            channel="telegram",
            ttl_minutes=settings.telegram_link_token_ttl_minutes,
        )
    url = (
        f"https://t.me/{settings.telegram_bot_username}?start=link_{plain}"
    )
    return LinkTokenResponse(
        url=url, expires_in_minutes=settings.telegram_link_token_ttl_minutes,
    )


# --- /webhook endpoint (Telegram → us) ------------------------------------


@router.post("/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> Response:
    settings = get_settings()
    expected = settings.telegram_webhook_secret
    if expected and x_telegram_bot_api_secret_token != expected:
        raise HTTPException(403, "bad webhook secret")

    try:
        update = await request.json()
    except Exception:  # noqa: BLE001
        # Don't 500 on malformed JSON — Telegram retries, that's worse.
        log.warning("channels.telegram.webhook.bad_json")
        return Response(status_code=200)

    message = update.get("message") or update.get("edited_message")
    if message is None:
        # We ignore non-message updates for v1 (callback queries,
        # inline queries, etc.). Acknowledge so Telegram stops retrying.
        return Response(status_code=200)

    text = (message.get("text") or "").strip()
    from_user = message.get("from") or {}
    chat = message.get("chat") or {}
    tg_user_id = str(from_user.get("id") or "")
    tg_chat_id = str(chat.get("id") or tg_user_id)
    tg_username = from_user.get("username")
    if not tg_user_id or not text:
        return Response(status_code=200)

    # Onboarding: /start link_<token>
    if text.startswith(_LINK_PREFIX):
        plain = text[len(_LINK_PREFIX):].strip()
        await _handle_onboarding(
            tg_user_id=tg_user_id, tg_chat_id=tg_chat_id,
            tg_username=tg_username, plain_token=plain,
        )
        return Response(status_code=200)

    # Bare /start (no link payload) — friendly prompt.
    if text in ("/start", "/start "):
        await get_telegram_client().send_message(
            chat_id=tg_chat_id,
            text=(
                "Hi! To talk to your Wolfpaw, link this Telegram account first."
                " From the web app, generate a link and tap it here."
            ),
        )
        return Response(status_code=200)

    # Resolve the linked Wolfpaw user.
    async with acquire() as conn:
        user_id = await channel_links.find_user(
            conn, channel="telegram", external_id=tg_user_id,
        )
    if user_id is None:
        await get_telegram_client().send_message(
            chat_id=tg_chat_id,
            text=(
                "I don't recognize this Telegram account yet."
                " Generate a link from the web app and tap it here to connect."
            ),
        )
        return Response(status_code=200)

    # Fire-and-forget — Telegram needs a fast ack.
    asyncio.create_task(
        _handle_inbound(
            user_id=user_id, tg_chat_id=tg_chat_id, content=text,
        )
    )
    return Response(status_code=200)


# --- handlers --------------------------------------------------------------


async def _handle_onboarding(
    *, tg_user_id: str, tg_chat_id: str, tg_username: str | None,
    plain_token: str,
) -> None:
    """Validate the deep-link token, write the channel_links row, send
    confirmation. Errors are surfaced to the user via Telegram rather
    than as HTTP errors (Telegram retried anyway)."""
    client = get_telegram_client()
    try:
        async with acquire() as conn:
            user_id = await consume_link_token(
                conn, plain=plain_token, channel="telegram",
            )
            await channel_links.create(
                conn,
                user_id=user_id, channel="telegram",
                external_id=tg_user_id, external_username=tg_username,
                metadata={"chat_id": tg_chat_id},
            )
    except LinkTokenError as e:
        log.info("channels.telegram.onboarding.bad_token", reason=str(e))
        await client.send_message(
            chat_id=tg_chat_id,
            text=(
                f"That link didn't work: {e}. Generate a fresh link from"
                " the web app and try again."
            ),
        )
        return
    except Exception as e:  # noqa: BLE001 — including UniqueViolation
        # Most likely: this Telegram account is already linked to a user.
        log.warning(
            "channels.telegram.onboarding.failed",
            tg_user_id=tg_user_id, exc_info=True,
        )
        await client.send_message(
            chat_id=tg_chat_id,
            text=(
                "Couldn't link this account — it may already be connected"
                " to another Wolfpaw user. Contact support if that's"
                " unexpected."
            ),
        )
        return

    log.info(
        "channels.telegram.onboarding.success",
        user_id=str(user_id), tg_user_id=tg_user_id,
    )
    await client.send_message(
        chat_id=tg_chat_id,
        text=(
            "You're linked. Send me anything to chat. Slash commands:"
            " /help, /usage, /tasks, /task <id>, /cancel <id>."
        ),
    )


async def _handle_inbound(
    *, user_id: UUID, tg_chat_id: str, content: str,
) -> None:
    """Run the user's message through the dispatcher → Router and
    push the response back via sendMessage. Used as a background task
    so the webhook returns 200 fast."""
    client = get_telegram_client()
    try:
        inbound = await _channel.receive({
            "user_id": user_id, "content": content,
        })
        cmd_result = await get_dispatcher().dispatch(inbound)
        if cmd_result is not None:
            await client.send_message(chat_id=tg_chat_id, text=cmd_result.text)
            return

        # Resolve or extend the user's most-recent Telegram thread.
        async with acquire() as conn:
            thread_id = await _resolve_telegram_thread(conn, user_id)

        ctx = ToolContext(user_id=user_id)
        final = await get_router().handle(
            ctx=ctx, thread_id=thread_id, content=content, emit=None,
        )
        await client.send_message(chat_id=tg_chat_id, text=final)
    except Exception as e:  # noqa: BLE001
        log.exception(
            "channels.telegram.inbound_failed", user_id=str(user_id),
        )
        try:
            await client.send_message(
                chat_id=tg_chat_id,
                text=f"Sorry, I ran into a problem: {e}",
            )
        except Exception:  # noqa: BLE001 — final fallback
            pass


async def _resolve_telegram_thread(conn, user_id: UUID) -> UUID:
    """Return the user's most recent Telegram thread, creating one if
    none exists. v1 doesn't have `/reset` for Telegram — the thread
    grows until cleared via a future command."""
    existing = await conn.fetchval(
        "SELECT id FROM threads WHERE user_id = $1 AND channel = 'telegram'"
        " ORDER BY created_at DESC LIMIT 1",
        user_id,
    )
    if existing is not None:
        return existing
    return await conv.get_or_create_thread(
        conn, user_id=user_id, channel="telegram",
    )
