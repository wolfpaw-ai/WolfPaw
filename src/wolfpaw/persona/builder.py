"""Assemble the full system prompt for an agent call.

Composition order (top-down — the model reads top first):

    # Wolfpaw — Soul File
    <soul.md content>
    ---
    # The user you're working with
    <user_profile.persona_md>
    Timezone: <tz>
    Structured preferences:
      - <k>: <v>
      ...
    ---
    # Your role
    <agent_role>          ← the per-agent prompt (Triage / Quick / etc.)

The Soul + user-profile sections degrade gracefully when missing
(file not found, DB unavailable, etc.) — the agent still gets its role
prompt. Tests can `set_soul_for_test(...)` to control prompt content
without filesystem I/O.

`build_for_agent(...)` is the convenience wrapper agents use: pass a
user_id + role string, it loads Soul + profile + assembles.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from wolfpaw.memory.db import acquire
from wolfpaw.persona import user_profile as up
from wolfpaw.persona.soul import Soul, get_soul
from wolfpaw.persona.user_profile import DEFAULT_PROFILE, UserProfile
from wolfpaw.tracing import get_logger

log = get_logger()


def _format_preferences(preferences: dict[str, Any]) -> str:
    if not preferences:
        return ""
    lines = ["Structured preferences:"]
    for k, v in sorted(preferences.items()):
        lines.append(f"- {k}: {v}")
    return "\n".join(lines)


def _format_user_block(profile: UserProfile) -> str:
    lines = ["# The user you're working with"]
    persona = (profile.persona_md or "").strip()
    if persona:
        lines.append(persona)
    else:
        lines.append("(this user hasn't filled in their User File yet)")
    extras: list[str] = []
    if profile.timezone and profile.timezone != "UTC":
        extras.append(f"Timezone: {profile.timezone}")
    prefs_block = _format_preferences(profile.preferences)
    if prefs_block:
        extras.append(prefs_block)
    if extras:
        lines.append("")
        lines.append("\n\n".join(extras))
    return "\n".join(lines)


def _format_soul_block(soul: Soul | None) -> str:
    if soul is None or not soul.content.strip():
        return ""
    return f"# Wolfpaw — Soul File\n\n{soul.content.strip()}"


def build_system_prompt(
    *,
    soul: Soul | None,
    user_profile: UserProfile,
    agent_role: str,
) -> str:
    """Pure assembly — no I/O. Useful for tests + for the runtime helper."""
    parts: list[str] = []
    soul_block = _format_soul_block(soul)
    if soul_block:
        parts.append(soul_block)
    parts.append(_format_user_block(user_profile))
    parts.append(f"# Your role\n\n{agent_role.strip()}")
    return "\n\n---\n\n".join(parts)


async def build_for_agent(*, user_id: UUID, agent_role: str) -> str:
    """Runtime helper used by every agent's call path. Falls back to a
    bare agent_role on Soul / DB load failures so the call still goes
    through in a degraded state."""
    try:
        soul: Soul | None = get_soul()
    except Exception:  # noqa: BLE001 — degraded mode is fine
        log.warning("persona.soul.load_failed", exc_info=True)
        soul = None
    profile = DEFAULT_PROFILE
    try:
        async with acquire() as conn:
            loaded = await up.get(conn, user_id=user_id)
        if loaded is not None:
            profile = loaded
    except Exception:  # noqa: BLE001
        log.warning("persona.profile.load_failed", exc_info=True)
    return build_system_prompt(
        soul=soul, user_profile=profile, agent_role=agent_role,
    )
