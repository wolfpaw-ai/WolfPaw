"""Where voice devices are connected, and how messages reach them.

A device's WebSocket lives in one API process, but the code that wants to
speak to it can run anywhere: a scheduled task in an arq worker calls
`VoiceChannel.send`, and the worker has no socket. So delivery goes through a
`VoiceBus`:

- `LocalBus` — one process (workers off, the dev default). Delivers straight
  to the hub in this process.
- `RedisBus` — several processes (workers on). Each API process publishes its
  connected devices to a presence hash in Redis; senders read that to choose
  a device, then publish the message on a Redis channel that every API
  process listens to. The process holding that device's socket delivers it.

Presence entries carry a timestamp and go stale after
`voice_presence_ttl_seconds`, so a crashed API process doesn't leave phantom
devices behind. Devices ping every ~30 s, which refreshes it.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol
from uuid import UUID

from fastapi import WebSocket

from wolfpaw.config import get_settings
from wolfpaw.tracing import get_logger

log = get_logger()

# Control message the hub understands instead of forwarding to the device.
REVOKE = "__revoke__"
CLOSE_REVOKED = 4001


@dataclass
class DeviceInfo:
    """What a sender needs to know to choose a device."""

    device_id: str
    user_id: str
    name: str
    audio_ok: bool = True
    last_active: float = 0.0  # epoch seconds of the last message the user spoke here


def pick_device(devices: list[DeviceInfo], prefer: str | None = None) -> DeviceInfo | None:
    """The device a server-initiated message should go to: the named one if
    it can speak, else the one the user spoke to most recently, else any that
    can speak. Devices whose audio is unavailable are skipped."""
    usable = [d for d in devices if d.audio_ok]
    if not usable:
        return None
    if prefer is not None:
        for d in usable:
            if d.device_id == prefer:
                return d
    return max(usable, key=lambda d: d.last_active)


class DeviceConn:
    """One connected device, as held by the API process that owns its socket."""

    def __init__(self, *, device_id: UUID, user_id: UUID, name: str, ws: WebSocket) -> None:
        self.device_id = device_id
        self.user_id = user_id
        self.name = name
        self.ws = ws
        self.audio_ok = True
        self.last_active = 0.0
        self._lock = asyncio.Lock()

    def info(self) -> DeviceInfo:
        return DeviceInfo(
            device_id=str(self.device_id),
            user_id=str(self.user_id),
            name=self.name,
            audio_ok=self.audio_ok,
            last_active=self.last_active,
        )

    async def send(self, message: dict[str, Any]) -> None:
        # One writer at a time: replies from parallel turns share the socket.
        async with self._lock:
            await self.ws.send_json(message)


class VoiceHub:
    """The sockets held by this process."""

    def __init__(self) -> None:
        self._conns: dict[str, DeviceConn] = {}

    def register(self, conn: DeviceConn) -> None:
        old = self._conns.get(str(conn.device_id))
        self._conns[str(conn.device_id)] = conn
        if old is not None and old is not conn:
            # The same device reconnected before the old socket was noticed dead.
            asyncio.ensure_future(_quiet_close(old.ws, 1000))

    def unregister(self, conn: DeviceConn) -> None:
        if self._conns.get(str(conn.device_id)) is conn:
            del self._conns[str(conn.device_id)]

    def get(self, device_id: str) -> DeviceConn | None:
        return self._conns.get(device_id)

    def for_user(self, user_id: str) -> list[DeviceConn]:
        return [c for c in self._conns.values() if str(c.user_id) == user_id]

    async def deliver(self, device_id: str, message: dict[str, Any]) -> bool:
        """Send to a device held by this process. False if it isn't here."""
        conn = self._conns.get(device_id)
        if conn is None:
            return False
        if message.get("type") == REVOKE:
            await _quiet_close(conn.ws, CLOSE_REVOKED)
            return True
        try:
            await conn.send(message)
        except Exception:  # noqa: BLE001 — a dead socket is cleaned up by its own loop
            log.warning("voice.hub.deliver_failed", device_id=device_id, exc_info=True)
            return False
        return True


async def _quiet_close(ws: WebSocket, code: int) -> None:
    try:
        await ws.close(code=code)
    except Exception:  # noqa: BLE001 — already closed
        pass


class VoiceBus(Protocol):
    async def start(self, hub: VoiceHub) -> None: ...
    async def stop(self) -> None: ...
    async def update_presence(self, info: DeviceInfo) -> None: ...
    async def remove_presence(self, user_id: str, device_id: str) -> None: ...
    async def devices_for_user(self, user_id: str) -> list[DeviceInfo]: ...
    async def publish(self, device_id: str, message: dict[str, Any]) -> bool: ...


class LocalBus:
    """Single process: the hub is the only place devices can be."""

    def __init__(self, hub: VoiceHub) -> None:
        self._hub = hub

    async def start(self, hub: VoiceHub) -> None:
        self._hub = hub

    async def stop(self) -> None:
        return None

    async def update_presence(self, info: DeviceInfo) -> None:
        return None  # the hub already knows

    async def remove_presence(self, user_id: str, device_id: str) -> None:
        return None

    async def devices_for_user(self, user_id: str) -> list[DeviceInfo]:
        return [c.info() for c in self._hub.for_user(user_id)]

    async def publish(self, device_id: str, message: dict[str, Any]) -> bool:
        return await self._hub.deliver(device_id, message)


class RedisBus:
    """Several processes: presence in a Redis hash, messages over pub/sub."""

    PRESENCE_PREFIX = "wolfpaw:voice:presence:"
    OUT_CHANNEL = "wolfpaw:voice:out"

    def __init__(self, redis: Any, *, presence_ttl_seconds: int | None = None) -> None:
        self._redis = redis
        self._ttl = presence_ttl_seconds or get_settings().voice_presence_ttl_seconds
        self._task: asyncio.Task | None = None
        self._pubsub: Any = None

    async def start(self, hub: VoiceHub) -> None:
        """Listen for messages addressed to devices this process holds. Only
        API processes (which hold sockets) call this; workers just publish."""
        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(self.OUT_CHANNEL)
        self._task = asyncio.create_task(self._listen(hub))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self._pubsub is not None:
            try:
                await self._pubsub.unsubscribe(self.OUT_CHANNEL)
                await self._pubsub.close()
            except Exception:  # noqa: BLE001
                pass
            self._pubsub = None

    async def _listen(self, hub: VoiceHub) -> None:
        async for item in self._pubsub.listen():
            if item.get("type") != "message":
                continue
            try:
                payload = json.loads(item["data"])
                await hub.deliver(payload["device_id"], payload["message"])
            except Exception:  # noqa: BLE001 — one bad message can't stop the listener
                log.warning("voice.bus.bad_message", exc_info=True)

    def _key(self, user_id: str) -> str:
        return f"{self.PRESENCE_PREFIX}{user_id}"

    async def update_presence(self, info: DeviceInfo) -> None:
        entry = {**asdict(info), "seen": time.time()}
        await self._redis.hset(self._key(info.user_id), info.device_id, json.dumps(entry))
        await self._redis.expire(self._key(info.user_id), self._ttl * 4)

    async def remove_presence(self, user_id: str, device_id: str) -> None:
        await self._redis.hdel(self._key(user_id), device_id)

    async def devices_for_user(self, user_id: str) -> list[DeviceInfo]:
        raw = await self._redis.hgetall(self._key(user_id))
        now = time.time()
        devices: list[DeviceInfo] = []
        for value in raw.values():
            try:
                entry = json.loads(value)
                if now - float(entry["seen"]) > self._ttl:
                    continue
                devices.append(DeviceInfo(
                    device_id=entry["device_id"],
                    user_id=entry["user_id"],
                    name=entry["name"],
                    audio_ok=bool(entry["audio_ok"]),
                    last_active=float(entry["last_active"]),
                ))
            except (KeyError, ValueError, TypeError):
                continue
        return devices

    async def publish(self, device_id: str, message: dict[str, Any]) -> bool:
        receivers = await self._redis.publish(
            self.OUT_CHANNEL, json.dumps({"device_id": device_id, "message": message}),
        )
        return bool(receivers)


# --- process-wide instances -------------------------------------------------

_hub = VoiceHub()
_bus: VoiceBus | None = None


def get_hub() -> VoiceHub:
    return _hub


def get_bus() -> VoiceBus:
    """The bus for this process: Redis when workers are enabled, otherwise
    in-process. Created on first use."""
    global _bus
    if _bus is None:
        settings = get_settings()
        if settings.workers_enabled:
            from redis import asyncio as aioredis

            _bus = RedisBus(aioredis.from_url(settings.redis_url, decode_responses=True))
        else:
            _bus = LocalBus(_hub)
    return _bus


def set_bus(bus: VoiceBus | None) -> None:
    """Test hook (and for callers that build their own bus)."""
    global _bus
    _bus = bus
