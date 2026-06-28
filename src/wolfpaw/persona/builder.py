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

Caching: `build_for_agent` reads the profile from Postgres on every
call, and a single user can drive a single Router turn through many
agent calls (Triage → Planner → many Executor steps → Post-Eval). We
cache the loaded `UserProfile` per-user with a short TTL to keep the
DAO calls down to one per request burst. `invalidate_profile(user_id)`
must be called whenever the row is mutated (the PATCH endpoint does
this); for multi-process safety the TTL ensures stale entries clear on
their own within `_PROFILE_TTL_SECONDS`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from wolfpaw.memory.db import acquire
from wolfpaw.persona import user_profile as up
from wolfpaw.persona.soul import Soul, get_soul
from wolfpaw.persona.user_profile import DEFAULT_PROFILE, UserProfile
from wolfpaw.tracing import get_logger

log = get_logger()


# Short — long enough to coalesce one user's request burst, short enough
# that cross-process staleness from a PATCH on another worker self-heals
# within seconds. PATCH on this process is invalidated synchronously.
_PROFILE_TTL_SECONDS = 30.0


@dataclass(frozen=True)
class _CachedProfile:
    profile: UserProfile
    expires_at: float


_profile_cache: dict[UUID, _CachedProfile] = {}


def invalidate_profile(user_id: UUID) -> None:
    """Drop the cached profile for this user. Called after PATCH so the
    next agent call sees the updated row immediately on this process."""
    _profile_cache.pop(user_id, None)


def clear_profile_cache() -> None:
    """Test hook — wipe the whole cache between cases."""
    _profile_cache.clear()


async def _load_profile_cached(user_id: UUID) -> UserProfile:
    now = time.monotonic()
    cached = _profile_cache.get(user_id)
    if cached is not None and cached.expires_at > now:
        return cached.profile
    async with acquire() as conn:
        loaded = await up.get(conn, user_id=user_id)
    profile = loaded if loaded is not None else DEFAULT_PROFILE
    _profile_cache[user_id] = _CachedProfile(
        profile=profile, expires_at=now + _PROFILE_TTL_SECONDS,
    )
    return profile


def _format_preferences(preferences: dict[str, Any]) -> str:
    if not preferences:
        return ""
    lines = ["Structured preferences:"]
    for k, v in sorted(preferences.items()):
        lines.append(f"- {k}: {v}")
    return "\n".join(lines)


def _format_now(profile: UserProfile, now: datetime) -> str:
    """Render the current instant in the user's timezone (plus UTC) so the
    agent can reason about clock times like "8pm" and compute relative
    offsets. The user's tz lets "tonight"/"tomorrow" resolve correctly."""
    tz_name = profile.timezone or "UTC"
    try:
        zone = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 — bad tz falls back to UTC
        zone, tz_name = timezone.utc, "UTC"
    local = now.astimezone(zone)
    utc = now.astimezone(timezone.utc)
    return (
        f"Current time: {local:%Y-%m-%d %H:%M} ({tz_name}); "
        f"{utc:%Y-%m-%dT%H:%M:%SZ} UTC."
    )


def _format_user_block(profile: UserProfile, now: datetime | None = None) -> str:
    lines = ["# The user you're working with"]
    persona = (profile.persona_md or "").strip()
    if persona:
        lines.append(persona)
    else:
        lines.append("(this user hasn't filled in their User File yet)")
    extras: list[str] = []
    if now is not None:
        extras.append(_format_now(profile, now))
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
    now: datetime | None = None,
) -> str:
    """Pure assembly — no I/O. Useful for tests + for the runtime helper.

    ``now`` (a timezone-aware datetime) is rendered into the user block so
    the agent knows the current time; omit it for deterministic prompt
    assertions."""
    parts: list[str] = []
    soul_block = _format_soul_block(soul)
    if soul_block:
        parts.append(soul_block)
    parts.append(_format_user_block(user_profile, now))
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
    try:
        profile = await _load_profile_cached(user_id)
    except Exception:  # noqa: BLE001
        log.warning("persona.profile.load_failed", exc_info=True)
        profile = DEFAULT_PROFILE
    return build_system_prompt(
        soul=soul, user_profile=profile, agent_role=agent_role,
        now=datetime.now(timezone.utc),
    )
