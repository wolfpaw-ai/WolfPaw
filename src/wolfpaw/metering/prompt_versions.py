"""Accessor + bump helper for `prompt_versions`.

Each `token_usage` row references the version active at the time of the call,
so we can ask "what did planner v8 do that v7 didn't" in raw SQL or via
LangSmith eval datasets.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import asyncpg

from wolfpaw.metering.types import PromptVersionRow


def _hash_template(content_template: dict[str, Any]) -> str:
    canonical = json.dumps(content_template, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def get_active_prompt_version(
    conn: asyncpg.Connection, agent: str
) -> PromptVersionRow | None:
    """Return the most-recent version row for the agent, or None if no version
    has been seeded yet (the wrapper records NULL in that case)."""
    row = await conn.fetchrow(
        "SELECT id, agent::text AS agent, version_label, content_hash,"
        "       content_template"
        "  FROM prompt_versions"
        " WHERE agent = $1::agent_kind"
        " ORDER BY created_at DESC LIMIT 1",
        agent,
    )
    if row is None:
        return None
    return PromptVersionRow(
        id=row["id"],
        agent=row["agent"],
        version_label=row["version_label"],
        content_hash=row["content_hash"],
        content_template=json.loads(row["content_template"])
        if isinstance(row["content_template"], str)
        else row["content_template"],
    )


async def bump_prompt_version(
    conn: asyncpg.Connection,
    *,
    agent: str,
    version_label: str,
    content_template: dict[str, Any],
) -> PromptVersionRow:
    """Insert a new version row. Idempotent on (agent, version_label)."""
    content_hash = _hash_template(content_template)
    row = await conn.fetchrow(
        "INSERT INTO prompt_versions (agent, version_label, content_hash,"
        "                              content_template)"
        " VALUES ($1::agent_kind, $2, $3, $4::jsonb)"
        " ON CONFLICT (agent, version_label)"
        "   DO UPDATE SET content_hash = EXCLUDED.content_hash,"
        "                 content_template = EXCLUDED.content_template"
        " RETURNING id, agent::text AS agent, version_label, content_hash,"
        "           content_template",
        agent,
        version_label,
        content_hash,
        json.dumps(content_template),
    )
    assert row is not None
    return PromptVersionRow(
        id=row["id"],
        agent=row["agent"],
        version_label=row["version_label"],
        content_hash=row["content_hash"],
        content_template=json.loads(row["content_template"])
        if isinstance(row["content_template"], str)
        else row["content_template"],
    )
