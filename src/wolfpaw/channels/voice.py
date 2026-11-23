"""Voice channel — paired devices (a Raspberry Pi with a headset or speaker)
over a WebSocket. See `voice-integration.md` for the protocol.

    POST   /channels/voice/pair-code        — Profile page → us (mint a code)
    POST   /channels/voice/pair             — device → us (trade code for token)
    GET    /channels/voice/devices          — Profile page: list paired devices
    DELETE /channels/voice/devices/{id}     — Profile page: revoke a device
    WS     /channels/voice/ws               — device, `Authorization: Bearer`

The device does wake word, speech-to-text and text-to-speech. This channel
exchanges plain text with it: utterances in; replies, questions and
announcements out. `VoiceChannel.send` is how anything that isn't a direct
reply (scheduled tasks, `ask_user` from a worker) reaches a device; it picks
a device, and falls back to Telegram if none can speak.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from wolfpaw.agents.router import get_router
from wolfpaw.auth.deps import require_user_id
from wolfpaw.channels import Channel, InboundMessage, get_channel, register_channel
from wolfpaw.channels.telegram_tokens import LinkTokenError, _hash as _hash_code, consume as consume_code
from wolfpaw.channels.voice_hub import (
    CLOSE_REVOKED,
    REVOKE,
    DeviceConn,
    get_bus,
    get_hub,
    pick_device,
)
from wolfpaw.config import get_settings
from wolfpaw.memory import (
    conversational as conv,
    pending_questions as pq_dao,
    voice_devices,
)
from wolfpaw.memory.db import acquire
from wolfpaw.toolbox.registry import ToolContext
from wolfpaw.tracing import get_logger

log = get_logger()
router = APIRouter(prefix="/channels/voice", tags=["channels"])

PROTOCOL_VERSION = 1
CLOSE_BAD_PROTOCOL = 4002

# Pairing codes are read off a screen and typed or spoken, so they avoid
# look-alike characters (no 0/O, 1/I/L).
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8

# Spoken replies must stay short; this is the stand-in when a turn fails.
_FAILURE_REPLY = "Sorry, something went wrong on my end."


def new_pair_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def normalize_code(raw: str) -> str:
    return "".join(ch for ch in raw.upper() if ch.isalnum())


def format_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


# --- The channel ------------------------------------------------------------


class VoiceChannel(Channel):
    name = "voice"

    async def receive(self, payload: dict) -> InboundMessage:
        return InboundMessage(
            user_id=payload["user_id"],
            content=payload["content"],
            channel_name=self.name,
            thread_id=payload.get("thread_id"),
            metadata=payload.get("metadata", {}),
        )

    async def send(self, user_id: UUID, content: str, **kwargs: Any) -> None:
        """Say something to the user on one of their devices.

        Optional keyword arguments:
            question_id   deliver as a question the device will answer
            expires_at    epoch seconds; the device drops it after that
            priority      "info" (default) or "alarm"
            message_id    id the device acknowledges
            device_id     send to this device instead of the usual choice

        Falls back to Telegram when no device can speak.
        """
        sent = await self._send_to_device(user_id, content, **kwargs)
        if sent:
            return

        log.info("channels.voice.send.fallback", user_id=str(user_id))
        telegram = get_channel("telegram")
        if telegram is None:
            return
        try:
            await telegram.send(user_id, content)
        except Exception:  # noqa: BLE001 — nothing more we can do
            log.warning("channels.voice.send.fallback_failed", exc_info=True)

    async def _send_to_device(self, user_id: UUID, content: str, **kwargs: Any) -> bool:
        bus = get_bus()
        devices = await bus.devices_for_user(str(user_id))
        target = pick_device(devices, prefer=kwargs.get("device_id"))
        if target is None:
            return False

        question_id = kwargs.get("question_id")
        expires_at = kwargs.get("expires_at")
        if question_id is not None:
            message: dict[str, Any] = {
                "type": "question",
                "question_id": str(question_id),
                "text": content,
            }
        else:
            message = {
                "type": "notify",
                "id": str(kwargs.get("message_id") or uuid4()),
                "text": content,
                "priority": kwargs.get("priority") or "info",
            }
        if expires_at is not None:
            message["expires_at"] = _iso(expires_at)

        return await bus.publish(target.device_id, message)

    async def send_timer(
        self, user_id: UUID, *, seconds: int, label: str, device_id: str | None = None,
    ) -> bool:
        """Start a countdown that runs on the device. False if no device can take it."""
        bus = get_bus()
        target = pick_device(await bus.devices_for_user(str(user_id)), prefer=device_id)
        if target is None:
            return False
        return await bus.publish(target.device_id, {
            "type": "set_timer", "id": str(uuid4()), "seconds": int(seconds), "label": label,
        })

    def supports_streaming(self) -> bool:
        return False  # replies are whole sentences, spoken by the device


_channel = register_channel(VoiceChannel())


def _iso(value: Any) -> str:
    """Epoch seconds, a datetime, or an ISO string → ISO 8601 (UTC)."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    return str(value)


# --- Pairing ----------------------------------------------------------------


class PairCodeResponse(BaseModel):
    code: str
    expires_in_minutes: int


class PairRequest(BaseModel):
    code: str
    device_name: str = "Voice device"


class PairResponse(BaseModel):
    device_token: str
    device_id: UUID


class DeviceOut(BaseModel):
    id: UUID
    name: str
    created_at: datetime
    last_seen_at: datetime | None
    connected: bool


class _AttemptLimiter:
    """At most `limit` pairing attempts per client per minute, so an 8-character
    code can't be guessed by brute force within its lifetime."""

    def __init__(self, limit: int = 10, window: float = 60.0) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        if len(hits) >= self.limit:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True


_pair_limiter = _AttemptLimiter()


@router.post("/pair-code", response_model=PairCodeResponse)
async def mint_pair_code(user_id: UUID = Depends(require_user_id)) -> PairCodeResponse:
    settings = get_settings()
    code = new_pair_code()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.voice_pair_code_ttl_minutes)
    async with acquire() as conn:
        await conn.execute(
            "INSERT INTO channel_link_tokens (user_id, channel, token_hash, expires_at)"
            " VALUES ($1, 'voice'::channel, $2, $3)",
            user_id, _hash_code(code), expires_at,
        )
    return PairCodeResponse(code=format_code(code), expires_in_minutes=settings.voice_pair_code_ttl_minutes)


@router.post("/pair", response_model=PairResponse)
async def pair(payload: PairRequest, request: Request) -> PairResponse:
    client = request.client.host if request.client else "unknown"
    if not _pair_limiter.allow(client):
        raise HTTPException(429, "too many pairing attempts; wait a minute")

    name = payload.device_name.strip()[:60] or "Voice device"
    try:
        async with acquire() as conn:
            user_id = await consume_code(
                conn, plain=normalize_code(payload.code), channel="voice",
            )
            device, token = await voice_devices.create(conn, user_id=user_id, name=name)
    except LinkTokenError as e:
        raise HTTPException(400, f"that code didn't work: {e}")

    log.info("channels.voice.paired", user_id=str(user_id), device_id=str(device.id))
    return PairResponse(device_token=token, device_id=device.id)


@router.get("/devices", response_model=list[DeviceOut])
async def list_devices(user_id: UUID = Depends(require_user_id)) -> list[DeviceOut]:
    async with acquire() as conn:
        devices = await voice_devices.list_for_user(conn, user_id=user_id)
    online = {d.device_id for d in await get_bus().devices_for_user(str(user_id))}
    return [
        DeviceOut(
            id=d.id, name=d.name, created_at=d.created_at,
            last_seen_at=d.last_seen_at, connected=str(d.id) in online,
        )
        for d in devices
    ]


@router.delete("/devices/{device_id}", status_code=204)
async def revoke_device(device_id: UUID, user_id: UUID = Depends(require_user_id)) -> None:
    async with acquire() as conn:
        revoked = await voice_devices.revoke(conn, user_id=user_id, device_id=device_id)
    if not revoked:
        raise HTTPException(404, "no such device")
    # Close its socket wherever it is held.
    await get_bus().publish(str(device_id), {"type": REVOKE})


# --- WebSocket --------------------------------------------------------------


def _bearer(ws: WebSocket) -> str | None:
    header = ws.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


@router.websocket("/ws")
async def voice_socket(ws: WebSocket) -> None:
    await ws.accept()

    token = _bearer(ws)
    device = None
    if token:
        async with acquire() as conn:
            device = await voice_devices.find_by_token(conn, plain=token)
    if device is None:
        # Accept-then-close (rather than refusing the handshake) lets the device
        # tell "token revoked, pair again" from a network failure.
        await ws.close(code=CLOSE_REVOKED)
        return

    await _serve(ws, device)


async def _serve(ws: WebSocket, device: voice_devices.VoiceDevice) -> None:
    hub, bus = get_hub(), get_bus()
    conn = DeviceConn(device_id=device.id, user_id=device.user_id, name=device.name, ws=ws)
    turns: dict[str, asyncio.Task] = {}
    greeted = False

    try:
        while True:
            try:
                message = await ws.receive_json()
            except ValueError:
                continue  # not JSON; ignore the frame
            if not isinstance(message, dict):
                continue
            kind = message.get("type")

            if kind == "hello":
                if message.get("protocol") != PROTOCOL_VERSION:
                    await ws.send_json({"type": "error", "code": "unsupported_protocol"})
                    await ws.close(code=CLOSE_BAD_PROTOCOL)
                    return
                hub.register(conn)
                await bus.update_presence(conn.info())
                await _touch(device.id)
                await conn.send({"type": "welcome", "server_time": datetime.now(timezone.utc).isoformat()})
                greeted = True
                continue

            if not greeted:
                continue  # nothing is accepted before hello

            if kind == "ping":
                await bus.update_presence(conn.info())
                await conn.send({"type": "pong"})
            elif kind == "utterance":
                _start_turn(conn, turns, message)
            elif kind == "answer":
                await _handle_answer(conn, message)
            elif kind == "status":
                conn.audio_ok = message.get("audio") != "unavailable"
                await bus.update_presence(conn.info())
            elif kind == "cancel":
                task = turns.get(str(message.get("in_reply_to")))
                if task is not None:
                    task.cancel()
            elif kind in ("ack", "alarm_ack", "timer_fired"):
                log.info(f"channels.voice.{kind}", device_id=str(device.id), id=message.get("id"))
            # Unknown types are ignored so either side can add messages.
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 — a bad frame shouldn't take the server down
        log.warning("channels.voice.socket_error", device_id=str(device.id), exc_info=True)
    finally:
        for task in turns.values():
            task.cancel()
        hub.unregister(conn)
        if hub.get(str(device.id)) is None:
            await bus.remove_presence(str(device.user_id), str(device.id))


async def _touch(device_id: UUID) -> None:
    try:
        async with acquire() as db:
            await voice_devices.touch(db, device_id=device_id)
    except Exception:  # noqa: BLE001 — bookkeeping only
        log.warning("channels.voice.touch_failed", exc_info=True)


def _start_turn(conn: DeviceConn, turns: dict[str, asyncio.Task], message: dict[str, Any]) -> None:
    utterance_id = str(message.get("id") or uuid4())
    text = str(message.get("text") or "").strip()
    if not text:
        return
    conn.last_active = time.time()
    task = asyncio.create_task(_run_turn(conn, utterance_id, text))
    turns[utterance_id] = task
    task.add_done_callback(lambda _t, i=utterance_id: turns.pop(i, None))


async def _run_turn(conn: DeviceConn, utterance_id: str, text: str) -> None:
    """Run one utterance through the agent and speak the result."""
    progress_sent = False

    async def emit(event: str, data: str) -> None:
        nonlocal progress_sent
        if event == "ask_user":
            await _emit_question(conn, data)
            return
        # Any other event means the agent is working; say so once.
        if not progress_sent:
            progress_sent = True
            await conn.send({"type": "progress", "in_reply_to": utterance_id})

    try:
        await get_bus().update_presence(conn.info())  # records that the user just spoke here
        async with acquire() as db:
            thread_id = await conv.get_most_recent_thread(db, user_id=conn.user_id)
            if thread_id is None:
                thread_id = await conv.get_or_create_thread(db, user_id=conn.user_id, channel=_channel.name)

        ctx = ToolContext(user_id=conn.user_id, channel=_channel.name, emit=emit)
        reply = await get_router().handle(ctx=ctx, thread_id=thread_id, content=text, emit=emit)
        await conn.send({"type": "reply", "in_reply_to": utterance_id, "text": reply})
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — tell the user rather than leave them in silence
        log.exception("channels.voice.turn_failed", user_id=str(conn.user_id))
        try:
            await conn.send({"type": "reply", "in_reply_to": utterance_id, "text": _FAILURE_REPLY})
        except Exception:  # noqa: BLE001
            pass


async def _emit_question(conn: DeviceConn, data: str) -> None:
    """An `ask_user` event from a turn running for this device becomes a
    `question` message."""
    import json

    try:
        payload = json.loads(data)
        question_id = payload["question_id"]
        text = str(payload["question"])
    except (ValueError, KeyError, TypeError):
        log.warning("channels.voice.bad_ask_user_event")
        return

    options = payload.get("options") or []
    if options:
        text = f"{text} " + ", ".join(str(o) for o in options) + "?"

    message: dict[str, Any] = {"type": "question", "question_id": question_id, "text": text}
    try:
        async with acquire() as db:
            pq = await pq_dao.get(db, question_id=UUID(question_id))
        if pq is not None and pq.expires_at is not None:
            message["expires_at"] = _iso(pq.expires_at)
    except Exception:  # noqa: BLE001 — the expiry is optional
        pass
    await conn.send(message)


async def _handle_answer(conn: DeviceConn, message: dict[str, Any]) -> None:
    try:
        question_id = UUID(str(message.get("question_id")))
    except ValueError:
        return
    text = str(message.get("text") or "").strip()
    if not text:
        return
    conn.last_active = time.time()
    await get_bus().update_presence(conn.info())
    async with acquire() as db:
        outcome = await pq_dao.mark_answered(
            db, question_id=question_id, user_id=conn.user_id, answer=text,
        )
    if outcome is not pq_dao.AnswerOutcome.ANSWERED:
        log.info("channels.voice.answer_not_applied", outcome=outcome.value, question_id=str(question_id))
