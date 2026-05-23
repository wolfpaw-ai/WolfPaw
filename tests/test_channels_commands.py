"""Unit tests for the slash-command dispatcher."""

from __future__ import annotations

from uuid import uuid4

import pytest

from wolfpaw.channels import InboundMessage
from wolfpaw.channels.commands import (
    CommandDispatcher,
    CommandResult,
    get_dispatcher,
)


def _msg(content: str) -> InboundMessage:
    return InboundMessage(
        user_id=uuid4(), content=content, channel_name="web"
    )


async def test_plain_message_returns_none():
    result = await get_dispatcher().dispatch(_msg("hello world"))
    assert result is None


async def test_help_lists_registered_commands():
    result = await get_dispatcher().dispatch(_msg("/help"))
    assert isinstance(result, CommandResult)
    assert "Available commands:" in result.text
    assert "/help" in result.text


async def test_unknown_command_returns_hint_not_none():
    """Unknown slash commands must not fall through to the agent pipeline."""
    result = await get_dispatcher().dispatch(_msg("/notarealcommand"))
    assert isinstance(result, CommandResult)
    assert "Unknown command" in result.text
    assert "/help" in result.text


async def test_leading_whitespace_tolerated():
    result = await get_dispatcher().dispatch(_msg("   /help"))
    assert isinstance(result, CommandResult)
    assert "Available commands:" in result.text


async def test_lone_slash_is_not_a_command():
    """`/` by itself is plain content, not a malformed command."""
    result = await get_dispatcher().dispatch(_msg("/"))
    assert result is None


async def test_custom_dispatcher_passes_args_to_handler():
    captured: dict = {}

    async def handler(msg: InboundMessage, args: str) -> CommandResult:
        captured["args"] = args
        captured["user_id"] = msg.user_id
        return CommandResult(text=f"echo:{args}")

    d = CommandDispatcher()
    d.register("echo", "Echo arguments back.", handler)
    msg = _msg("/echo hello world  ")
    result = await d.dispatch(msg)
    assert result == CommandResult(text="echo:hello world")
    assert captured["args"] == "hello world"
    assert captured["user_id"] == msg.user_id


async def test_register_normalizes_leading_slash():
    d = CommandDispatcher()

    async def h(msg: InboundMessage, args: str) -> CommandResult:
        return CommandResult(text="ok")

    d.register("/with-slash", "demo", h)
    assert any(c.name == "with-slash" for c in d.commands())


async def test_register_rejects_empty_name():
    d = CommandDispatcher()

    async def h(msg: InboundMessage, args: str) -> CommandResult:
        return CommandResult(text="x")

    with pytest.raises(ValueError):
        d.register("/", "empty", h)
