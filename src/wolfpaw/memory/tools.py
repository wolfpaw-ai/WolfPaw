"""DAO for the ``tools`` table — agent-proposed, human-approved
user-tools (step 28).

Builtin tools (the in-process Registry in :mod:`wolfpaw.toolbox.registry`)
live in code and are never written here. This DAO is exclusively for
runtime-emitted, user-scoped tools that the Tool Creator agent
proposes, the user approves via ``ask_user``, and the Executor
dispatches at execution time when no builtin covers the name.

State machine for a user-tool row:

    (insert) → proposed
       proposed → approved  (user said yes; the row becomes dispatchable)
       proposed → rejected  (user said no; kept for audit, not dispatchable)

Only ``approved`` rows surface from :func:`find_active_by_name` /
:func:`list_approved_for_user`. ``rejected`` rows are kept so the
Planner can be told "we already tried this and you turned it down" on
future similar gaps (a v3 affordance — for now they just sit there).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import asyncpg


ToolStatus = Literal["proposed", "approved", "rejected"]


@dataclass(frozen=True)
class UserTool:
    id: UUID
    user_id: UUID
    name: str
    description: str
    signature: dict[str, Any]
    implementation: str
    status: ToolStatus
    source_plan_id: UUID | None = None
    source_task_id: UUID | None = None
    created_at: datetime | None = None
    approved_at: datetime | None = None
    similarity: float | None = None  # set by search_by_task


def _row_to_user_tool(
    row: asyncpg.Record, similarity: float | None = None,
) -> UserTool:
    signature = row["signature"]
    if isinstance(signature, str):
        signature = json.loads(signature)
    return UserTool(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        description=row["description"],
        signature=dict(signature or {}),
        implementation=row["implementation"] or "",
        status=row["status"],
        source_plan_id=row["source_plan_id"],
        source_task_id=row["source_task_id"],
        created_at=row["created_at"],
        approved_at=row["approved_at"],
        similarity=similarity,
    )


async def store_proposed(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    name: str,
    description: str,
    signature: dict[str, Any],
    implementation: str,
    embedding: list[float] | None,
    source_plan_id: UUID | None,
    source_task_id: UUID | None,
) -> UUID:
    """Insert a freshly-proposed tool. Returns the new row id."""
    return await conn.fetchval(
        """
        INSERT INTO tools (
            user_id, name, description, signature, embedding,
            implementation, status, source_plan_id, source_task_id
        )
        VALUES ($1, $2, $3, $4::jsonb, $5, $6, 'proposed', $7, $8)
        RETURNING id
        """,
        user_id, name, description, json.dumps(signature),
        embedding, implementation, source_plan_id, source_task_id,
    )


async def mark_approved(
    conn: asyncpg.Connection, *, tool_id: UUID,
) -> bool:
    """Approve a proposed tool. Returns True iff the row transitioned
    from 'proposed' (idempotent — repeat calls return False)."""
    result = await conn.execute(
        """
        UPDATE tools
           SET status = 'approved', approved_at = NOW()
         WHERE id = $1 AND status = 'proposed'
        """,
        tool_id,
    )
    return result.endswith(" 1")


async def mark_rejected(
    conn: asyncpg.Connection, *, tool_id: UUID,
) -> bool:
    """Reject a proposed tool. Returns True iff the row transitioned
    from 'proposed'."""
    result = await conn.execute(
        """
        UPDATE tools
           SET status = 'rejected'
         WHERE id = $1 AND status = 'proposed'
        """,
        tool_id,
    )
    return result.endswith(" 1")


async def find_active_by_name(
    conn: asyncpg.Connection, *, user_id: UUID, name: str,
) -> UserTool | None:
    """The Executor's dispatch lookup: return the user's approved tool
    of this name, or None. Returns None for proposed / rejected rows
    too — only ``status='approved'`` is dispatchable."""
    row = await conn.fetchrow(
        """
        SELECT id, user_id, name, description, signature, implementation,
               status, source_plan_id, source_task_id,
               created_at, approved_at
          FROM tools
         WHERE user_id = $1 AND name = $2 AND status = 'approved'
         LIMIT 1
        """,
        user_id, name,
    )
    return _row_to_user_tool(row) if row else None


async def list_approved_for_user(
    conn: asyncpg.Connection, *, user_id: UUID,
) -> list[UserTool]:
    """All of this user's approved tools, oldest first. The Planner
    inlines the names + descriptions into its prompt so it can pick
    them alongside builtins."""
    rows = await conn.fetch(
        """
        SELECT id, user_id, name, description, signature, implementation,
               status, source_plan_id, source_task_id,
               created_at, approved_at
          FROM tools
         WHERE user_id = $1 AND status = 'approved'
         ORDER BY created_at ASC
        """,
        user_id,
    )
    return [_row_to_user_tool(r) for r in rows]


async def search_by_task(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    query_embedding: list[float],
    k: int = 5,
) -> list[UserTool]:
    """Top-k user-approved tools matching the embedding by cosine
    similarity. Used by the Tool Creator to detect near-duplicates
    before proposing a new one (same pattern as Skills dedup)."""
    rows = await conn.fetch(
        """
        SELECT id, user_id, name, description, signature, implementation,
               status, source_plan_id, source_task_id,
               created_at, approved_at,
               1 - (embedding <=> $2) AS similarity
          FROM tools
         WHERE user_id = $1 AND status = 'approved'
           AND embedding IS NOT NULL
         ORDER BY embedding <=> $2
         LIMIT $3
        """,
        user_id, query_embedding, k,
    )
    return [
        _row_to_user_tool(r, similarity=float(r["similarity"]))
        for r in rows
    ]
