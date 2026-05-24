"""Slash-command dispatcher — handles `/help`, `/usage`, `/tasks`, … without
burning model tokens.

Channels intercept inbound messages here *before* routing to the agent
pipeline. Commands are registered via the `@register(...)` decorator so each
step that owns a command can colocate registration with the handler (e.g.
`/usage` registers from `metering/usage_report.py` in step 6).

The registry is process-global; importing a module with a registration
decorator is sufficient to make the command callable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from wolfpaw.channels import InboundMessage


@dataclass(frozen=True)
class CommandResult:
    text: str
    # Channels that maintain client-side thread state (web SSE) read this to
    # know they should drop the current thread_id, so the next user message
    # starts a fresh conversation. Channels without client thread state
    # (Telegram) ignore the flag; those handlers do the reset server-side.
    clear_thread: bool = False


CommandHandler = Callable[[InboundMessage, str], Awaitable[CommandResult]]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    handler: CommandHandler


class CommandDispatcher:
    def __init__(self) -> None:
        self._commands: dict[str, CommandSpec] = {}

    def register(
        self, name: str, description: str, handler: CommandHandler
    ) -> None:
        key = name.lstrip("/").lower()
        if not key:
            raise ValueError("command name cannot be empty")
        self._commands[key] = CommandSpec(
            name=key, description=description, handler=handler
        )

    def commands(self) -> list[CommandSpec]:
        return sorted(self._commands.values(), key=lambda c: c.name)

    async def dispatch(self, message: InboundMessage) -> CommandResult | None:
        """Run the matching command if the message is a slash command.

        Returns None when the message is plain content (so the caller falls
        through to the agent pipeline). Returns a CommandResult — including
        an "unknown command" hint — when the message starts with `/`, so
        unknown commands never leak through to the model.
        """
        content = message.content.strip()
        if not content.startswith("/") or len(content) < 2:
            return None
        parts = content[1:].split(maxsplit=1)
        if not parts:
            return None
        name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        spec = self._commands.get(name)
        if spec is None:
            return CommandResult(
                text=f"Unknown command: /{name}. Try /help."
            )
        return await spec.handler(message, args)


_dispatcher = CommandDispatcher()


def get_dispatcher() -> CommandDispatcher:
    return _dispatcher


def register(
    name: str, description: str
) -> Callable[[CommandHandler], CommandHandler]:
    """Decorator: register a handler under `name` (with or without leading slash)."""

    def decorator(handler: CommandHandler) -> CommandHandler:
        _dispatcher.register(name, description, handler)
        return handler

    return decorator


# --- built-in commands -------------------------------------------------------


@register("help", "List available slash commands.")
async def _help(message: InboundMessage, args: str) -> CommandResult:
    lines = ["Available commands:"]
    for spec in _dispatcher.commands():
        lines.append(f"  /{spec.name} — {spec.description}")
    return CommandResult(text="\n".join(lines))


@register("reset", "Start a fresh conversation thread.")
async def _reset(message: InboundMessage, args: str) -> CommandResult:
    """Reset the user's current conversation thread.

    Web (and any client-driven channel) signals via `clear_thread=True`
    so the client forgets its thread_id; the next message creates a
    fresh thread on the server. Telegram has no client-side thread
    state — the channel always resolves the user's most-recent thread —
    so we mint an empty new thread now and the resolver will pick it
    up for the next message.
    """
    if message.channel_name == "telegram":
        # Lazy import — avoids loading the DB layer at command-registration
        # time (which happens at import).
        from wolfpaw.memory import conversational as conv
        from wolfpaw.memory.db import acquire

        try:
            async with acquire() as conn:
                await conv.get_or_create_thread(
                    conn, user_id=message.user_id, channel="telegram",
                    thread_id=None,
                )
        except Exception:  # noqa: BLE001
            return CommandResult(
                text="Couldn't reset the thread right now. Try again in a moment.",
            )
    return CommandResult(
        text="Thread reset. Your next message starts a fresh conversation.",
        clear_thread=True,
    )
