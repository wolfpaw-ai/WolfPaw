"""Slack channel — OAuth install, Events API webhook (DMs), slash commands.

Routes:
    GET   /channels/slack/install-url      — authed Wolfpaw user; returns
                                             the "Add to Slack" URL
    GET   /channels/slack/oauth/callback   — Slack redirects here after
                                             the workspace admin approves
    POST  /channels/slack/events           — Events API (DMs + URL verification)
    POST  /channels/slack/commands         — slash command dispatch

Install flow:
    1. Authenticated Wolfpaw user hits /install-url. We mint a single-use
       state token via `channels.telegram_tokens.issue` (same shape — the
       `channel_link_tokens` table is shared) bound to their user_id.
    2. Response: an OAuth URL with `state=<token>` + `client_id` +
       `scope=app_mentions:read,chat:write,commands,im:history,im:read,im:write`.
    3. User clicks, Slack approves, redirects to /oauth/callback?code=...&state=...
    4. We consume the state token → Wolfpaw user_id; exchange code via
       `oauth.v2.access` → bot token + team + slack user_id; persist the
       `slack_workspaces` row + `channel_links` row for that Wolfpaw user.

Inbound flow (events):
    1. Slack POSTs to /events. We verify the signature against the raw
       body before parsing.
    2. URL verification challenge: echo back the `challenge` value.
    3. `message.im` (DM to the bot): look up the workspace's bot token,
       resolve `(team_id, user_id)` → Wolfpaw user via channel_links,
       fire-and-forget the Router, push the reply back via chat.postMessage.
    4. Other event types are acknowledged with 200 and ignored.

Inbound flow (slash command `/wolfpaw <text>`):
    1. POST to /commands, form-encoded. Verify signature.
    2. Resolve `(team_id, user_id)` → Wolfpaw user. If unlinked, return
       a friendly help message.
    3. Dispatch via the shared command dispatcher (matches `/help`, `/usage`,
       `/tasks`, `/reset` text after stripping the `/wolfpaw` prefix);
       for free-form text, fire-and-forget the Router and return an
       ephemeral "working on it" ack — the real reply arrives via
       chat.postMessage when the Router finishes.

The fire-and-forget pattern is the same v1 trade as web + Telegram: if
the process dies mid-task the user never sees a reply.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse, PlainTextResponse, Response

from wolfpaw.agents.router import get_router
from wolfpaw.auth.deps import require_user_id
from wolfpaw.channels import Channel, InboundMessage
from wolfpaw.channels.commands import get_dispatcher
from wolfpaw.channels.slack_client import (
    SlackApiError,
    SlackClient,
    get_slack_client,
)
from wolfpaw.channels.slack_signing import BadSignature, verify as verify_signature
from wolfpaw.channels.telegram_tokens import (
    LinkTokenError,
    consume as consume_state_token,
    issue as issue_state_token,
)
from wolfpaw.config import get_settings
from wolfpaw.memory import channel_links, conversational as conv
from wolfpaw.memory import slack_workspaces
from wolfpaw.memory.db import acquire
from wolfpaw.toolbox.registry import ToolContext
from wolfpaw.tracing import get_logger

log = get_logger()
router = APIRouter(prefix="/channels/slack", tags=["channels"])

# Scopes we request at install time. `commands` for slash commands,
# `im:*` for DMs, `chat:write` for replies, `app_mentions:read` so
# @-mentions in channels become events (v1.5 — DMs are the main path
# today but the scope is cheap to claim now).
_OAUTH_SCOPES = (
    "app_mentions:read,chat:write,commands,im:history,im:read,im:write"
)


# --- Channel adapter -----------------------------------------------------


class SlackChannel(Channel):
    name = "slack"

    async def receive(self, payload: dict) -> InboundMessage:
        return InboundMessage(
            user_id=payload["user_id"],
            content=payload["content"],
            channel_name=self.name,
            thread_id=payload.get("thread_id"),
            metadata=payload.get("metadata", {}),
        )

    async def send(self, user_id: UUID, content: str, **kwargs: Any) -> None:
        """Proactive push from server → user. Looks up the user's most
        recent Slack workspace link, posts to their DM channel.
        kwargs: optional `channel` override (e.g. for replying in a
        channel rather than DM)."""
        async with acquire() as conn:
            links = await channel_links.list_for_user(conn, user_id=user_id)
        slack_link = next(
            (link for link in links if link.channel == "slack"), None,
        )
        if slack_link is None:
            log.warning(
                "channels.slack.send.not_linked", user_id=str(user_id),
            )
            return
        team_id, _, slack_user_id = slack_link.external_id.partition(":")
        async with acquire() as conn:
            workspace = await slack_workspaces.get(conn, team_id=team_id)
        if workspace is None:
            log.warning(
                "channels.slack.send.workspace_revoked",
                user_id=str(user_id), team_id=team_id,
            )
            return
        # Default: DM the user (channel id == their slack user id is the
        # documented way to address the bot's IM channel with them).
        target = kwargs.get("channel") or slack_user_id
        client = get_slack_client()
        try:
            await client.chat_post_message(
                bot_token=workspace.bot_token,
                channel=target,
                text=content,
            )
        except SlackApiError:
            log.exception("channels.slack.send.failed",
                          user_id=str(user_id), team_id=team_id)

    def supports_streaming(self) -> bool:
        return False


_channel = SlackChannel()


# --- /install-url (web client → us) --------------------------------------


class InstallUrlResponse(BaseModel):
    url: str
    expires_in_minutes: int


@router.get("/install-url", response_model=InstallUrlResponse)
async def install_url(
    user_id: UUID = Depends(require_user_id),
) -> InstallUrlResponse:
    settings = get_settings()
    if not settings.slack_client_id:
        raise HTTPException(503, "Slack client ID not configured")
    async with acquire() as conn:
        state = await issue_state_token(
            conn,
            user_id=user_id,
            channel="slack",
            ttl_minutes=settings.slack_install_token_ttl_minutes,
        )
    redirect_uri = _redirect_uri(settings)
    url = (
        "https://slack.com/oauth/v2/authorize"
        f"?client_id={settings.slack_client_id}"
        f"&scope={_OAUTH_SCOPES}"
        f"&redirect_uri={redirect_uri}"
        f"&state={state}"
    )
    return InstallUrlResponse(
        url=url,
        expires_in_minutes=settings.slack_install_token_ttl_minutes,
    )


# --- /oauth/callback (Slack → us) ----------------------------------------


@router.get("/oauth/callback")
async def oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    """Slack redirects here after the workspace admin approves install.

    Three failure modes the user can see:
      - Slack returned `error=access_denied` (user cancelled)
      - bad/expired state (we don't know who started this install)
      - oauth.v2.access rejected the code

    On success we render a small HTML page with a "Return to Wolfpaw"
    link. The browser is no longer on the Wolfpaw origin (this is the
    Slack-driven redirect), so we can't bounce them automatically into
    the SPA — give them a link instead.
    """
    if error or not code or not state:
        return _callback_page(
            ok=False, message=f"Slack install was not completed: {error or 'missing code/state'}",
        )

    settings = get_settings()
    try:
        async with acquire() as conn:
            wolfpaw_user_id = await consume_state_token(
                conn, plain=state, channel="slack",
            )
    except LinkTokenError as e:
        log.info("channels.slack.oauth.bad_state", reason=str(e))
        return _callback_page(
            ok=False,
            message="Install link expired or already used. Generate a fresh link from the web app.",
        )

    client = get_slack_client()
    try:
        oauth = await client.oauth_v2_access(
            code=code,
            client_id=settings.slack_client_id,
            client_secret=settings.slack_client_secret,
            redirect_uri=_redirect_uri(settings),
        )
    except SlackApiError as e:
        log.warning("channels.slack.oauth.exchange_failed", error=e.error)
        return _callback_page(
            ok=False, message=f"Slack rejected the install: {e.error}",
        )

    team = oauth.get("team") or {}
    bot_user_id = oauth.get("bot_user_id") or ""
    bot_token = oauth.get("access_token") or ""
    authed_user = oauth.get("authed_user") or {}
    slack_user_id = authed_user.get("id") or ""
    team_id = team.get("id") or ""
    if not (team_id and bot_token and bot_user_id and slack_user_id):
        log.warning(
            "channels.slack.oauth.missing_fields", payload_keys=list(oauth.keys()),
        )
        return _callback_page(
            ok=False, message="Slack returned an incomplete OAuth response.",
        )

    external_id = f"{team_id}:{slack_user_id}"
    try:
        async with acquire() as conn:
            await slack_workspaces.upsert(
                conn,
                team_id=team_id,
                team_name=team.get("name"),
                bot_user_id=bot_user_id,
                bot_token=bot_token,
                installed_by_user_id=wolfpaw_user_id,
            )
            # Tolerate re-install: if the (channel, external_id) row
            # already exists we leave it alone. UniqueViolation surfaces
            # only when *another* Wolfpaw user has already claimed this
            # Slack identity.
            try:
                await channel_links.create(
                    conn,
                    user_id=wolfpaw_user_id, channel="slack",
                    external_id=external_id,
                    external_username=authed_user.get("name"),
                    metadata={"team_id": team_id, "slack_user_id": slack_user_id},
                )
            except Exception as e:  # noqa: BLE001
                # UniqueViolation on (channel, external_id) — most likely
                # the same user re-installing; safe to ignore.
                log.info(
                    "channels.slack.oauth.link_already_exists",
                    user_id=str(wolfpaw_user_id), team_id=team_id,
                    reason=str(e),
                )
    except Exception:  # noqa: BLE001
        log.exception("channels.slack.oauth.persist_failed")
        return _callback_page(
            ok=False, message="Couldn't save the Slack workspace. Try again.",
        )

    log.info(
        "channels.slack.oauth.success",
        user_id=str(wolfpaw_user_id), team_id=team_id,
    )
    return _callback_page(
        ok=True, message=f"Wolfpaw is now installed in {team.get('name') or team_id}.",
    )


# --- /events (Slack → us) -------------------------------------------------


@router.post("/events")
async def events(
    request: Request,
    x_slack_request_timestamp: str | None = Header(default=None),
    x_slack_signature: str | None = Header(default=None),
) -> Response:
    settings = get_settings()
    body = await request.body()
    try:
        verify_signature(
            signing_secret=settings.slack_signing_secret,
            timestamp=x_slack_request_timestamp,
            signature=x_slack_signature,
            body=body,
        )
    except BadSignature as e:
        log.info("channels.slack.events.bad_signature", reason=str(e))
        raise HTTPException(403, "bad signature")

    try:
        payload = json.loads(body)
    except Exception:  # noqa: BLE001
        log.warning("channels.slack.events.bad_json")
        return Response(status_code=200)

    # 1. URL verification handshake — Slack pings the endpoint with a
    # `challenge` value when an admin configures the Events API URL.
    if payload.get("type") == "url_verification":
        return PlainTextResponse(payload.get("challenge", ""))

    if payload.get("type") != "event_callback":
        return Response(status_code=200)

    event = payload.get("event") or {}
    event_type = event.get("type")
    team_id = payload.get("team_id") or ""
    if event_type == "message":
        await _handle_message_event(team_id=team_id, event=event)

    # Always 200 — Slack retries 5xx aggressively, which usually makes
    # the situation worse than swallowing the event silently.
    return Response(status_code=200)


# --- /commands (Slack → us) ----------------------------------------------


@router.post("/commands")
async def commands(
    request: Request,
    x_slack_request_timestamp: str | None = Header(default=None),
    x_slack_signature: str | None = Header(default=None),
) -> Response:
    settings = get_settings()
    body = await request.body()
    try:
        verify_signature(
            signing_secret=settings.slack_signing_secret,
            timestamp=x_slack_request_timestamp,
            signature=x_slack_signature,
            body=body,
        )
    except BadSignature as e:
        log.info("channels.slack.commands.bad_signature", reason=str(e))
        raise HTTPException(403, "bad signature")

    # Slash command payloads are application/x-www-form-urlencoded. We
    # parse the raw bytes ourselves (avoids pulling in python-multipart
    # just for this single endpoint).
    form = parse_qs(body.decode("utf-8", errors="replace"))
    team_id = (form.get("team_id") or [""])[0]
    slack_user_id = (form.get("user_id") or [""])[0]
    channel_id = (form.get("channel_id") or [""])[0]
    text = (form.get("text") or [""])[0].strip()

    if not (team_id and slack_user_id and channel_id):
        return _ephemeral("Missing team/user/channel — Slack payload was malformed.")

    async with acquire() as conn:
        user_id = await channel_links.find_user(
            conn, channel="slack", external_id=f"{team_id}:{slack_user_id}",
        )
    if user_id is None:
        return _ephemeral(
            "Your Slack account isn't linked to Wolfpaw yet. Install the app from the web UI to connect."
        )

    # Slash command text can be empty (`/wolfpaw` with no args) — treat
    # as `/help` for friendliness.
    if not text:
        text = "/help"

    inbound = await _channel.receive({
        "user_id": user_id,
        "content": text if text.startswith("/") else text,
        "metadata": {"team_id": team_id, "channel_id": channel_id},
    })

    cmd_result = await get_dispatcher().dispatch(inbound)
    if cmd_result is not None:
        return _ephemeral(cmd_result.text)

    # Free-form text: fire-and-forget the Router. Acknowledge inline so
    # Slack doesn't time out (3-second response window).
    async with acquire() as conn:
        workspace = await slack_workspaces.get(conn, team_id=team_id)
    if workspace is None:
        return _ephemeral(
            "This workspace's Wolfpaw install was revoked. Re-install from the web app."
        )

    asyncio.create_task(
        _handle_freeform(
            user_id=user_id, workspace_bot_token=workspace.bot_token,
            channel_id=channel_id, content=text,
        )
    )
    return _ephemeral("Working on it…")


# --- handlers -------------------------------------------------------------


async def _handle_message_event(*, team_id: str, event: dict) -> None:
    """Process a `message` event from the Events API. Only DMs to the
    bot (`channel_type == 'im'`) are routed today; channel messages need
    explicit @-mentions which we'll wire as a follow-up.

    Filters out bot-authored messages (including our own replies) by
    checking `bot_id` / `subtype` — we don't want infinite loops if the
    bot ever ends up in a thread with itself.
    """
    if event.get("bot_id") or event.get("subtype") in {"bot_message", "message_changed"}:
        return
    if event.get("channel_type") != "im":
        return

    slack_user_id = event.get("user") or ""
    channel_id = event.get("channel") or ""
    text = (event.get("text") or "").strip()
    if not (team_id and slack_user_id and channel_id and text):
        return

    async with acquire() as conn:
        user_id = await channel_links.find_user(
            conn, channel="slack", external_id=f"{team_id}:{slack_user_id}",
        )
        # Always fetch the workspace — we need its bot token either to
        # send the real reply (linked user) or the "install from web"
        # prompt (unlinked user).
        workspace = await slack_workspaces.get(conn, team_id=team_id)

    client = get_slack_client()
    if user_id is None or workspace is None:
        # Unlinked user OR revoked workspace. Try to be helpful if we at
        # least have a workspace token to reply with.
        if workspace is not None:
            try:
                await client.chat_post_message(
                    bot_token=workspace.bot_token, channel=channel_id,
                    text=(
                        "I don't recognize this Slack account yet. Install"
                        " Wolfpaw from the web app to connect."
                    ),
                )
            except SlackApiError:
                pass
        return

    asyncio.create_task(
        _handle_freeform(
            user_id=user_id, workspace_bot_token=workspace.bot_token,
            channel_id=channel_id, content=text,
        )
    )


async def _handle_freeform(
    *, user_id: UUID, workspace_bot_token: str, channel_id: str, content: str,
) -> None:
    """Run the user's message through the Router and push the response
    back via chat.postMessage. Background task — must catch its own
    exceptions or asyncio swallows them."""
    client = get_slack_client()
    try:
        # Resolve / extend this user's most-recent Slack thread.
        async with acquire() as conn:
            thread_id = await _resolve_slack_thread(conn, user_id)

        ctx = ToolContext(user_id=user_id)
        final = await get_router().handle(
            ctx=ctx, thread_id=thread_id, content=content, emit=None,
        )
        await client.chat_post_message(
            bot_token=workspace_bot_token, channel=channel_id, text=final,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("channels.slack.inbound_failed", user_id=str(user_id))
        try:
            await client.chat_post_message(
                bot_token=workspace_bot_token, channel=channel_id,
                text=f"Sorry, I ran into a problem: {e}",
            )
        except Exception:  # noqa: BLE001
            pass


async def _resolve_slack_thread(conn, user_id: UUID) -> UUID:
    """Same pattern as Telegram: a single rolling thread per user,
    extended on each new inbound. `/reset` mints a new one."""
    existing = await conn.fetchval(
        "SELECT id FROM threads WHERE user_id = $1 AND channel = 'slack'"
        " ORDER BY created_at DESC LIMIT 1",
        user_id,
    )
    if existing is not None:
        return existing
    return await conv.get_or_create_thread(
        conn, user_id=user_id, channel="slack",
    )


# --- helpers --------------------------------------------------------------


def _redirect_uri(settings) -> str:
    return f"{settings.web_base_url.rstrip('/')}/channels/slack/oauth/callback"


def _ephemeral(text: str) -> JSONResponse:
    """A slash-command response that only the invoking user sees in Slack."""
    return JSONResponse({"response_type": "ephemeral", "text": text})


def _callback_page(*, ok: bool, message: str) -> Response:
    """Tiny HTML page rendered after the OAuth redirect — the user is on
    the Wolfpaw origin via the Slack redirect, so we can show a real
    page rather than a JSON error."""
    title = "Slack connected" if ok else "Slack install didn't complete"
    status = 200 if ok else 400
    body = f"""<!doctype html>
<html><head><title>{title}</title>
<style>body{{font-family:-apple-system,Segoe UI,sans-serif;max-width:32rem;
margin:4rem auto;padding:0 1rem;line-height:1.5}}</style>
</head><body>
<h1>{title}</h1>
<p>{message}</p>
<p><a href="/">Return to Wolfpaw</a></p>
</body></html>"""
    return Response(content=body, status_code=status, media_type="text/html")
