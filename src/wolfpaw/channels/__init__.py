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
