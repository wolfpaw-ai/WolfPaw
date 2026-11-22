"""`announce` and `set_timer` — speak on the user's voice device, with the
Telegram fallback. The bus and the channels are stubbed."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from wolfpaw.channels import voice
from wolfpaw.channels.voice_hub import DeviceConn, LocalBus, VoiceHub, set_bus
from wolfpaw.persona.builder import build_system_prompt, channel_hint
from wolfpaw.toolbox.registry import ToolContext, ToolError, get_registry

USER = uuid4()


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)

    async def close(self, code=1000):
        pass


@pytest.fixture
def world(monkeypatch):
    hub = VoiceHub()
    set_bus(LocalBus(hub))
    telegram = SimpleNamespace(sent=[])

    async def tg_send(user_id, content, **kwargs):
        telegram.sent.append(content)

    telegram.send = tg_send
    monkeypatch.setattr(voice, "get_channel", lambda name: telegram if name == "telegram" else None)

    def add_device():
        ws = FakeWS()
        hub.register(DeviceConn(device_id=uuid4(), user_id=USER, name="kitchen", ws=ws))
        return ws

    yield SimpleNamespace(add_device=add_device, telegram=telegram)
    set_bus(None)


def _ctx():
    return ToolContext(user_id=USER, channel="voice")


async def test_announce_speaks_on_the_device(world):
    ws = world.add_device()
    result = await get_registry().get("announce").run(_ctx(), text=" The laundry is done. ")
    assert ws.sent[0]["text"] == "The laundry is done."
    assert result["delivered_to"] == "voice device"
    assert world.telegram.sent == []


async def test_announce_alarm_with_expiry(world):
    ws = world.add_device()
    await get_registry().get("announce").run(_ctx(), text="Wake up", priority="alarm", expires_in_seconds=600)
    assert ws.sent[0]["priority"] == "alarm"
    assert "expires_at" in ws.sent[0]


async def test_announce_falls_back_to_telegram_and_says_so(world):
    result = await get_registry().get("announce").run(_ctx(), text="Dinner's ready.")
    assert world.telegram.sent == ["Dinner's ready."]
    assert "telegram" in result["delivered_to"]


@pytest.mark.parametrize("inputs", [
    {"text": ""},
    {"text": "hi", "priority": "loud"},
    {"text": "hi", "expires_in_seconds": 0},
])
async def test_announce_rejects_bad_input(world, inputs):
    with pytest.raises(ToolError):
        await get_registry().get("announce").run(_ctx(), **inputs)


async def test_set_timer_starts_a_device_countdown(world):
    ws = world.add_device()
    result = await get_registry().get("set_timer").run(_ctx(), seconds=600, label="The pasta is ready.")
    assert result == {"started": True, "seconds": 600, "label": "The pasta is ready."}
    assert ws.sent[0]["type"] == "set_timer" and ws.sent[0]["seconds"] == 600


async def test_set_timer_without_a_device_is_a_recoverable_error(world):
    with pytest.raises(ToolError, match="schedule_task"):
        await get_registry().get("set_timer").run(_ctx(), seconds=60, label="x")


@pytest.mark.parametrize("inputs", [
    {"seconds": 0, "label": "x"},
    {"seconds": 100000, "label": "x"},
    {"seconds": 60, "label": " "},
])
async def test_set_timer_rejects_bad_input(world, inputs):
    world.add_device()
    with pytest.raises(ToolError):
        await get_registry().get("set_timer").run(_ctx(), **inputs)


def test_voice_turns_get_a_spoken_style_hint_and_others_do_not():
    kwargs = dict(soul=None, user_profile=SimpleNamespace(persona_md="", timezone="UTC", preferences={}), agent_role="role")
    spoken = build_system_prompt(channel="voice", **kwargs)
    plain = build_system_prompt(channel="web", **kwargs)
    assert "spoken aloud" in spoken
    assert "No markdown" in spoken
    assert "spoken aloud" not in plain
    assert channel_hint(None) == "" and channel_hint("telegram") == ""
