"""Channel abstraction — every user-facing surface (web, Telegram, email, Slack)
implements the same interface so the agent pipeline never has to know which
medium a message came from.

`InboundMessage` is the normalized shape every channel produces. Concrete
channels are responsible for parsing their own payload format and for
rendering outbound text the way their medium expects (markdown tables for
web, code blocks for Telegram, plain text for email, etc).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass
class InboundMessage:
    user_id: UUID
    content: str
    channel_name: str
    thread_id: UUID | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class Channel(ABC):
    name: str

    @abstractmethod
    async def receive(self, payload: dict) -> InboundMessage:
        """Parse a channel-specific payload into a normalized InboundMessage."""

    @abstractmethod
    async def send(self, user_id: UUID, content: str, **kwargs: Any) -> None:
        """Send a proactive message to the user via this channel.

        Channels that only support pull (like web SSE) may raise
        NotImplementedError here until a push transport (websockets, web
        push) lands.
        """

    @abstractmethod
    def supports_streaming(self) -> bool:
        """True iff this channel can deliver partial responses incrementally."""


# --- channel registry ------------------------------------------------------
#
# `ask_user` needs to resolve a Channel instance by name to push a question
# proactively (Telegram/Slack) when there's no live stream to emit into. Each
# concrete channel registers its singleton at import; `get_channel` also does
# a lazy import of the known built-ins so it works in a worker process that
# never loaded the FastAPI routers.

_CHANNELS: dict[str, Channel] = {}

_BUILTIN_CHANNEL_MODULES = {
    "web": "wolfpaw.channels.web",
    "telegram": "wolfpaw.channels.telegram",
    "slack": "wolfpaw.channels.slack",
}


def register_channel(channel: Channel) -> Channel:
    """Register a channel singleton under its `name`. Called at module import."""
    _CHANNELS[channel.name] = channel
    return channel


def get_channel(name: str) -> Channel | None:
    """Return the registered channel for `name`, or None. Lazily imports the
    matching built-in module (which self-registers) if it isn't loaded yet."""
    channel = _CHANNELS.get(name)
    if channel is not None:
        return channel
    module = _BUILTIN_CHANNEL_MODULES.get(name)
    if module is None:
        return None
    import importlib

    importlib.import_module(module)
    return _CHANNELS.get(name)
