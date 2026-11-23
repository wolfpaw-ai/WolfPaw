"""Voice channel: pairing, the device WebSocket, device choice, delivery and
the Telegram fallback. DB access and the agent router are stubbed, so the
suite runs without Postgres or Redis."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from wolfpaw.api import create_app
from wolfpaw.auth.deps import require_user_id
from wolfpaw.channels import voice
from wolfpaw.channels.telegram_tokens import LinkTokenError
from wolfpaw.channels.voice_hub import (
    CLOSE_REVOKED,
    REVOKE,
    DeviceConn,
    DeviceInfo,
    LocalBus,
    RedisBus,
    VoiceHub,
    pick_device,
    set_bus,
)
from wolfpaw.memory import pending_questions as pq_dao, voice_devices

USER = uuid4()
DEVICE_ID = uuid4()


@asynccontextmanager
async def _fake_acquire():
    yield None


def _device(name: str = "kitchen", user: UUID = USER, device_id: UUID | None = None):
    return voice_devices.VoiceDevice(
        id=device_id or DEVICE_ID, user_id=user, name=name,
        created_at=datetime.now(timezone.utc), last_seen_at=None, revoked_at=None,
    )


class FakeRouter:
    """Stands in for the agent: records the call, optionally emits events."""

    def __init__(self, reply="It is three o'clock.", events=()):
        self.reply = reply
        self.events = list(events)
        self.calls = []
        self.fail = False

    async def handle(self, *, ctx, thread_id, content, emit=None):
        self.calls.append({"channel": ctx.channel, "content": content})
        if self.fail:
            raise RuntimeError("model down")
        for event, data in self.events:
            await emit(event, data)
        return self.reply


@pytest.fixture
def rig(monkeypatch):
    """A test client with stubbed DB, router and a fresh in-process bus."""
    hub = VoiceHub()
    bus = LocalBus(hub)
    set_bus(bus)
    monkeypatch.setattr("wolfpaw.channels.voice_hub._hub", hub)
    monkeypatch.setattr(voice, "get_hub", lambda: hub)

    router = FakeRouter()
    monkeypatch.setattr(voice, "acquire", _fake_acquire)
    monkeypatch.setattr(voice, "get_router", lambda: router)
    monkeypatch.setattr(voice.conv, "get_most_recent_thread", _async(uuid4()))
    monkeypatch.setattr(voice.conv, "get_or_create_thread", _async(uuid4()))

    tokens = {"good-token": _device()}

    async def find_by_token(conn, *, plain):
        return tokens.get(plain)

    async def touch(conn, *, device_id):
        return None

    monkeypatch.setattr(voice.voice_devices, "find_by_token", find_by_token)
    monkeypatch.setattr(voice.voice_devices, "touch", touch)

    app = create_app()
    app.dependency_overrides[require_user_id] = lambda: USER
    client = TestClient(app)
    yield SimpleNamespace(client=client, router=router, hub=hub, bus=bus, tokens=tokens, monkeypatch=monkeypatch)
    set_bus(None)


def _async(value):
    async def fn(*args, **kwargs):
        return value
    return fn


def _connect(rig, token="good-token"):
    return rig.client.websocket_connect(
        "/channels/voice/ws", headers={"Authorization": f"Bearer {token}"},
    )


def _hello(ws):
    ws.send_json({"type": "hello", "protocol": 1, "capabilities": ["speech"]})
    return ws.receive_json()


# --- pure helpers -------------------------------------------------------------


def test_pair_codes_are_unambiguous_and_formatted():
    code = voice.new_pair_code()
    assert len(code) == 8
    assert not set(code) & set("01OIL")
    assert voice.format_code("ABCD2345") == "ABCD-2345"
    assert voice.normalize_code(" abcd-2345 ") == "ABCD2345"


def _info(name, *, audio_ok=True, last_active=0.0):
    return DeviceInfo(device_id=name, user_id=str(USER), name=name, audio_ok=audio_ok, last_active=last_active)


def test_pick_device_prefers_named_then_most_recently_used():
    a, b, c = _info("a", last_active=1), _info("b", last_active=5), _info("c", last_active=3)
    assert pick_device([a, b, c]).device_id == "b"
    assert pick_device([a, b, c], prefer="c").device_id == "c"
    assert pick_device([a, b, c], prefer="missing").device_id == "b"


def test_pick_device_skips_devices_that_cannot_speak():
    a, b = _info("a", last_active=9, audio_ok=False), _info("b", last_active=1)
    assert pick_device([a, b]).device_id == "b"
    assert pick_device([a]) is None
    assert pick_device([]) is None


# --- the socket ---------------------------------------------------------------


def test_unknown_token_is_closed_with_the_revoked_code(rig):
    with _connect(rig, token="nope") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == CLOSE_REVOKED


def test_missing_token_is_closed_with_the_revoked_code(rig):
    with rig.client.websocket_connect("/channels/voice/ws") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == CLOSE_REVOKED


def test_hello_gets_welcome_with_server_time(rig):
    with _connect(rig) as ws:
        welcome = _hello(ws)
    assert welcome["type"] == "welcome"
    datetime.fromisoformat(welcome["server_time"])


def test_wrong_protocol_is_refused(rig):
    with _connect(rig) as ws:
        ws.send_json({"type": "hello", "protocol": 99})
        assert ws.receive_json()["code"] == "unsupported_protocol"
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == voice.CLOSE_BAD_PROTOCOL


def test_nothing_is_handled_before_hello(rig):
    with _connect(rig) as ws:
        ws.send_json({"type": "utterance", "id": "u0", "text": "too early"})
        _hello(ws)
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
    assert rig.router.calls == []


def test_utterance_gets_progress_then_reply_on_the_voice_channel(rig):
    rig.router.events = [("triage", "quick — simple")]
    with _connect(rig) as ws:
        _hello(ws)
        ws.send_json({"type": "utterance", "id": "u1", "text": "what time is it"})
        assert ws.receive_json() == {"type": "progress", "in_reply_to": "u1"}
        assert ws.receive_json() == {"type": "reply", "in_reply_to": "u1", "text": "It is three o'clock."}
    assert rig.router.calls == [{"channel": "voice", "content": "what time is it"}]


def test_progress_is_sent_once_per_utterance(rig):
    rig.router.events = [("triage", "a"), ("plan", "b"), ("tool", "c")]
    with _connect(rig) as ws:
        _hello(ws)
        ws.send_json({"type": "utterance", "id": "u1", "text": "do a thing"})
        kinds = [ws.receive_json()["type"] for _ in range(2)]
    assert kinds == ["progress", "reply"]


def test_a_failing_turn_still_gets_a_spoken_reply(rig):
    rig.router.fail = True
    with _connect(rig) as ws:
        _hello(ws)
        ws.send_json({"type": "utterance", "id": "u1", "text": "hello"})
        message = ws.receive_json()
    assert message["type"] == "reply"
    assert message["in_reply_to"] == "u1"
    assert "something went wrong" in message["text"]


def test_ask_user_becomes_a_question_and_the_answer_is_recorded(rig):
    qid = str(uuid4())
    payload = json.dumps({"question_id": qid, "question": "Order milk?", "options": ["yes", "no"], "urgency": "normal"})
    rig.router.events = [("ask_user", payload)]
    answered = []

    async def mark_answered(conn, *, question_id, user_id, answer):
        answered.append((question_id, user_id, answer))
        return pq_dao.AnswerOutcome.ANSWERED

    async def pq_get(conn, *, question_id):
        return SimpleNamespace(expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))

    rig.monkeypatch.setattr(voice.pq_dao, "mark_answered", mark_answered)
    rig.monkeypatch.setattr(voice.pq_dao, "get", pq_get)

    with _connect(rig) as ws:
        _hello(ws)
        ws.send_json({"type": "utterance", "id": "u1", "text": "order groceries"})
        question = ws.receive_json()
        assert question["type"] == "question"
        assert question["question_id"] == qid
        assert question["text"] == "Order milk? yes, no?"
        datetime.fromisoformat(question["expires_at"])

        ws.send_json({"type": "answer", "question_id": qid, "text": "yes please"})
        assert ws.receive_json()["type"] == "reply"  # the turn finishes
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"  # answer was handled before the ping

    assert answered == [(UUID(qid), USER, "yes please")]


def test_unknown_messages_and_bad_frames_are_ignored(rig):
    with _connect(rig) as ws:
        _hello(ws)
        ws.send_json({"type": "from_the_future", "x": 1})
        ws.send_text("this is not json")
        ws.send_json([1, 2, 3])
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_status_marks_audio_unavailable_and_presence_follows(rig):
    with _connect(rig) as ws:
        _hello(ws)
        ws.send_json({"type": "status", "audio": "unavailable"})
        ws.send_json({"type": "ping"})
        ws.receive_json()
        devices = asyncio.run(rig.bus.devices_for_user(str(USER)))
        assert [d.audio_ok for d in devices] == [False]
        ws.send_json({"type": "status", "audio": "ok"})
        ws.send_json({"type": "ping"})
        ws.receive_json()
        devices = asyncio.run(rig.bus.devices_for_user(str(USER)))
        assert [d.audio_ok for d in devices] == [True]
    assert rig.hub.for_user(str(USER)) == []


def test_ack_and_timer_messages_are_accepted(rig):
    with _connect(rig) as ws:
        _hello(ws)
        for message in (
            {"type": "ack", "id": "n1"},
            {"type": "alarm_ack", "id": "a1", "action": "stop"},
            {"type": "timer_fired", "id": "t1"},
        ):
            ws.send_json(message)
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_cancel_stops_a_running_turn(rig):
    started = asyncio.Event

    class Slow(FakeRouter):
        async def handle(self, **kwargs):
            await asyncio.sleep(30)
            return "never"

    rig.monkeypatch.setattr(voice, "get_router", lambda: Slow())
    with _connect(rig) as ws:
        _hello(ws)
        ws.send_json({"type": "utterance", "id": "u1", "text": "slow one"})
        ws.send_json({"type": "cancel", "in_reply_to": "u1"})
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"  # no reply arrives for u1


# --- delivery ---------------------------------------------------------------------


class FakeWS:
    def __init__(self):
        self.sent = []
        self.closed = None

    async def send_json(self, message):
        self.sent.append(message)

    async def close(self, code=1000):
        self.closed = code


def _register(hub, *, name="kitchen", audio_ok=True, last_active=0.0, user=USER):
    ws = FakeWS()
    conn = DeviceConn(device_id=uuid4(), user_id=user, name=name, ws=ws)
    conn.audio_ok = audio_ok
    conn.last_active = last_active
    hub.register(conn)
    return conn, ws


@pytest.fixture
def delivery(monkeypatch):
    hub = VoiceHub()
    set_bus(LocalBus(hub))
    telegram = SimpleNamespace(sent=[])

    async def tg_send(user_id, content, **kwargs):
        telegram.sent.append((user_id, content))

    telegram.send = tg_send
    monkeypatch.setattr(voice, "get_channel", lambda name: telegram if name == "telegram" else None)
    yield SimpleNamespace(hub=hub, telegram=telegram)
    set_bus(None)


async def test_send_goes_to_the_device_the_user_spoke_to_last(delivery):
    _, kitchen = _register(delivery.hub, name="kitchen", last_active=10)
    _, living = _register(delivery.hub, name="living room", last_active=50)
    await voice._channel.send(USER, "Your laundry is done.")
    assert kitchen.sent == []
    assert living.sent[0]["type"] == "notify"
    assert living.sent[0]["text"] == "Your laundry is done."
    assert living.sent[0]["priority"] == "info"
    assert delivery.telegram.sent == []


async def test_send_can_name_a_device_and_set_priority_and_expiry(delivery):
    kitchen_conn, kitchen = _register(delivery.hub, name="kitchen", last_active=10)
    _register(delivery.hub, name="living room", last_active=50)
    soon = datetime.now(timezone.utc) + timedelta(minutes=1)
    await voice._channel.send(USER, "Wake up", device_id=str(kitchen_conn.device_id), priority="alarm", expires_at=soon)
    message = kitchen.sent[0]
    assert message["priority"] == "alarm"
    assert datetime.fromisoformat(message["expires_at"]) == soon


async def test_send_with_a_question_id_becomes_a_question(delivery):
    _, ws = _register(delivery.hub)
    qid = uuid4()
    await voice._channel.send(USER, "Which one?", question_id=qid, expires_at=1_900_000_000)
    assert ws.sent[0]["type"] == "question"
    assert ws.sent[0]["question_id"] == str(qid)
    assert ws.sent[0]["expires_at"].startswith("2030-")


async def test_send_skips_devices_without_audio_then_falls_back_to_telegram(delivery):
    _, headset = _register(delivery.hub, name="headset", audio_ok=False)
    await voice._channel.send(USER, "Time to go.")
    assert headset.sent == []
    assert delivery.telegram.sent == [(USER, "Time to go.")]


async def test_send_with_no_devices_falls_back_to_telegram(delivery):
    await voice._channel.send(USER, "Anyone home?")
    assert delivery.telegram.sent == [(USER, "Anyone home?")]


async def test_send_ignores_other_users_devices(delivery):
    _, theirs = _register(delivery.hub, user=uuid4())
    await voice._channel.send(USER, "mine")
    assert theirs.sent == []
    assert delivery.telegram.sent == [(USER, "mine")]


async def test_set_timer_reaches_the_device(delivery):
    _, ws = _register(delivery.hub)
    assert await voice._channel.send_timer(USER, seconds=600, label="Pasta is ready.") is True
    assert ws.sent[0]["type"] == "set_timer"
    assert (ws.sent[0]["seconds"], ws.sent[0]["label"]) == (600, "Pasta is ready.")


async def test_set_timer_without_a_device_reports_failure(delivery):
    assert await voice._channel.send_timer(USER, seconds=60, label="x") is False


async def test_revoke_message_closes_the_socket_with_the_revoked_code(delivery):
    conn, ws = _register(delivery.hub)
    assert await delivery.hub.deliver(str(conn.device_id), {"type": REVOKE}) is True
    assert ws.closed == CLOSE_REVOKED
    assert ws.sent == []


# --- Redis bus (against an in-memory stand-in) -------------------------------------


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.subscribers = []

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    async def hdel(self, key, field):
        self.hashes.get(key, {}).pop(field, None)

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def expire(self, key, seconds):
        return True

    async def publish(self, channel, data):
        for q in self.subscribers:
            q.put_nowait({"type": "message", "data": data})
        return len(self.subscribers)

    def pubsub(self):
        return FakePubSub(self)


class FakePubSub:
    def __init__(self, redis):
        self.redis = redis
        self.queue = asyncio.Queue()

    async def subscribe(self, channel):
        self.redis.subscribers.append(self.queue)

    async def unsubscribe(self, channel):
        self.redis.subscribers.remove(self.queue)

    async def close(self):
        return None

    async def listen(self):
        while True:
            yield await self.queue.get()


async def test_redis_bus_presence_round_trip_and_staleness():
    redis = FakeRedis()
    bus = RedisBus(redis, presence_ttl_seconds=120)
    await bus.update_presence(_info("kitchen", last_active=7))
    devices = await bus.devices_for_user(str(USER))
    assert [(d.name, d.last_active, d.audio_ok) for d in devices] == [("kitchen", 7.0, True)]

    # An entry nobody has refreshed goes stale.
    key = bus._key(str(USER))
    stale = json.loads(redis.hashes[key]["kitchen"])
    stale["seen"] -= 1000
    redis.hashes[key]["kitchen"] = json.dumps(stale)
    assert await bus.devices_for_user(str(USER)) == []

    await bus.update_presence(_info("kitchen"))
    await bus.remove_presence(str(USER), "kitchen")
    assert await bus.devices_for_user(str(USER)) == []


async def test_redis_bus_delivers_published_messages_to_the_local_hub():
    redis = FakeRedis()
    api_bus = RedisBus(redis)          # the API process holding the socket
    worker_bus = RedisBus(redis)       # a worker with no sockets, only publishing
    hub = VoiceHub()
    conn, ws = _register(hub)
    await api_bus.start(hub)
    try:
        delivered = await worker_bus.publish(str(conn.device_id), {"type": "notify", "id": "1", "text": "hi", "priority": "info"})
        assert delivered is True
        for _ in range(50):
            if ws.sent:
                break
            await asyncio.sleep(0.01)
        assert ws.sent[0]["text"] == "hi"
    finally:
        await api_bus.stop()


async def test_redis_bus_publish_reports_no_listeners():
    assert await RedisBus(FakeRedis()).publish("any", {"type": "notify"}) is False


# --- pairing ---------------------------------------------------------------------------


@pytest.fixture
def pairing(monkeypatch):
    app = create_app()
    app.dependency_overrides[require_user_id] = lambda: USER
    monkeypatch.setattr(voice, "acquire", _fake_acquire)
    voice._pair_limiter._hits.clear()
    return SimpleNamespace(client=TestClient(app), monkeypatch=monkeypatch)


def test_pair_trades_a_code_for_a_token(pairing):
    seen = {}

    async def consume(conn, *, plain, channel):
        seen["code"], seen["channel"] = plain, channel
        return USER

    async def create(conn, *, user_id, name):
        seen["name"] = name
        return _device(name), "plaintext-token"

    pairing.monkeypatch.setattr(voice, "consume_code", consume)
    pairing.monkeypatch.setattr(voice.voice_devices, "create", create)

    response = pairing.client.post("/channels/voice/pair", json={"code": "abcd-2345", "device_name": "Kitchen Pi"})
    assert response.status_code == 200
    assert response.json()["device_token"] == "plaintext-token"
    assert seen == {"code": "ABCD2345", "channel": "voice", "name": "Kitchen Pi"}


def test_pair_rejects_a_bad_code(pairing):
    async def consume(conn, *, plain, channel):
        raise LinkTokenError("token expired")

    pairing.monkeypatch.setattr(voice, "consume_code", consume)
    response = pairing.client.post("/channels/voice/pair", json={"code": "WRONG123"})
    assert response.status_code == 400
    assert "expired" in response.json()["detail"]


def test_pair_is_rate_limited(pairing):
    async def consume(conn, *, plain, channel):
        raise LinkTokenError("invalid token")

    pairing.monkeypatch.setattr(voice, "consume_code", consume)
    statuses = [pairing.client.post("/channels/voice/pair", json={"code": "WRONG123"}).status_code for _ in range(12)]
    assert statuses[:10] == [400] * 10
    assert statuses[10:] == [429, 429]


def test_pair_code_can_only_be_minted_when_signed_in(pairing, monkeypatch):
    executed = []

    class Conn:
        async def execute(self, sql, *args):
            executed.append(args)

    @asynccontextmanager
    async def acquire():
        yield Conn()

    monkeypatch.setattr(voice, "acquire", acquire)
    response = pairing.client.post("/channels/voice/pair-code")
    assert response.status_code == 200
    body = response.json()
    assert len(body["code"]) == 9 and body["code"][4] == "-"
    assert executed[0][0] == USER

    anonymous = TestClient(create_app())
    assert anonymous.post("/channels/voice/pair-code").status_code == 401


def test_revoking_a_device_closes_its_socket(rig):
    async def revoke(conn, *, user_id, device_id):
        return True

    rig.monkeypatch.setattr(voice.voice_devices, "revoke", revoke)
    with _connect(rig) as ws:
        _hello(ws)
        assert rig.client.delete(f"/channels/voice/devices/{DEVICE_ID}").status_code == 204
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == CLOSE_REVOKED


def test_revoking_someone_elses_device_is_a_404(rig):
    async def revoke(conn, *, user_id, device_id):
        return False

    rig.monkeypatch.setattr(voice.voice_devices, "revoke", revoke)
    assert rig.client.delete(f"/channels/voice/devices/{uuid4()}").status_code == 404
