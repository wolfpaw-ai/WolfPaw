"""Unit tests for `persona.soul` (file loader + hash) and `persona.builder`
(pure system-prompt assembly). No DB needed."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from wolfpaw.persona.builder import build_system_prompt
from wolfpaw.persona.soul import (
    Soul,
    load_soul,
    reset_soul,
    set_soul_for_test,
)
from wolfpaw.persona.user_profile import DEFAULT_PROFILE, UserProfile


# --- soul ----------------------------------------------------------------


def test_load_soul_hashes_content(tmp_path):
    p = tmp_path / "soul.md"
    p.write_text("hello world")
    soul = load_soul(p)
    assert soul.content == "hello world"
    assert len(soul.version) == 12  # SHA-256 prefix
    # Same content → same version.
    assert load_soul(p).version == soul.version


def test_load_soul_version_changes_with_content(tmp_path):
    p = tmp_path / "soul.md"
    p.write_text("v1")
    v1 = load_soul(p).version
    p.write_text("v2")
    v2 = load_soul(p).version
    assert v1 != v2


def test_load_soul_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_soul(tmp_path / "nope.md")


def test_set_soul_for_test_overrides_cache():
    set_soul_for_test("under test")
    from wolfpaw.persona.soul import get_soul

    soul = get_soul()
    assert soul.content == "under test"
    assert soul.source == "(test override)"
    reset_soul()


# --- builder -------------------------------------------------------------


def _profile(**kw) -> UserProfile:
    base = dict(
        user_id=uuid4(), version=1, persona_md="",
        preferences={}, timezone="UTC",
    )
    base.update(kw)
    return UserProfile(**base)


def test_build_system_prompt_includes_all_three_blocks():
    soul = Soul(content="Soul says X.", version="abc", source="(test)")
    profile = _profile(persona_md="I am Alice. I prefer kilometers.")
    s = build_system_prompt(
        soul=soul, user_profile=profile,
        agent_role="You are the Quick Agent.",
    )
    assert "Soul says X." in s
    assert "I am Alice" in s
    assert "You are the Quick Agent." in s
    # Order is soul → user → role.
    assert s.index("Soul says X.") < s.index("I am Alice")
    assert s.index("I am Alice") < s.index("You are the Quick Agent.")


def test_build_system_prompt_handles_missing_soul():
    profile = _profile(persona_md="hi")
    s = build_system_prompt(
        soul=None, user_profile=profile, agent_role="role.",
    )
    assert "Wolfpaw — Soul" not in s
    assert "hi" in s
    assert "role." in s


def test_build_system_prompt_empty_persona_shows_placeholder():
    profile = _profile(persona_md="")
    s = build_system_prompt(
        soul=None, user_profile=profile, agent_role="role.",
    )
    assert "hasn't filled in their User File" in s


def test_build_system_prompt_renders_preferences_and_timezone():
    profile = _profile(
        persona_md="user",
        preferences={"formality": "casual", "units": "metric"},
        timezone="Asia/Tokyo",
    )
    s = build_system_prompt(
        soul=None, user_profile=profile, agent_role="role.",
    )
    assert "Timezone: Asia/Tokyo" in s
    assert "formality: casual" in s
    assert "units: metric" in s


def test_build_system_prompt_skips_default_utc_timezone_line():
    """The default timezone shouldn't add noise to the prompt."""
    profile = _profile(persona_md="x", timezone="UTC")
    s = build_system_prompt(
        soul=None, user_profile=profile, agent_role="role.",
    )
    assert "Timezone:" not in s
