"""Data access for the `workspace_files` table.

`workspace_files` is append-only: every overwrite inserts a new row with
`version = prior + 1` and `supersedes_id = prior.id`. The prior row stays
for history. Listings show the latest version per filename by default.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

import asyncpg

FileSource = Literal["user_upload", "agent_output"]


@dataclass(frozen=True)
class WorkspaceFile:
    id: UUID
    user_id: UUID
    task_id: UUID | None
    source: FileSource
    filename: str
    mime_type: str | None
    storage_url: str
    size_bytes: int
    version: int
    supersedes_id: UUID | None
    sha256: str | None
    created_at: datetime


class WorkspaceCollision(Exception):
    """Raised by write_doc when a filename already exists and `overwrite`
    wasn't requested. The executor's overwrite-with-confirmation flow
    catches this and transitions the task to `awaiting_user`.
    """

    def __init__(self, existing: WorkspaceFile) -> None:
        super().__init__(
            f"workspace file {existing.filename!r} already exists at v{existing.version}"
        )
        self.existing = existing


def _row_to_file(row: asyncpg.Record) -> WorkspaceFile:
    return WorkspaceFile(
        id=row["id"],
        user_id=row["user_id"],
        task_id=row["task_id"],
        source=row["source"],
        filename=row["filename"],
        mime_type=row["mime_type"],
        storage_url=row["storage_url"],
        size_bytes=row["size_bytes"],
        version=row["version"],
        supersedes_id=row["supersedes_id"],
        sha256=row["sha256"],
        created_at=row["created_at"],
    )


async def list_latest(
    conn: asyncpg.Connection, user_id: UUID
) -> list[WorkspaceFile]:
    rows = await conn.fetch(
        "SELECT DISTINCT ON (filename) id, user_id, task_id, source::text AS source,"
        "       filename, mime_type, storage_url, size_bytes, version,"
        "       supersedes_id, sha256, created_at"
        "  FROM workspace_files"
        " WHERE user_id = $1"
        " ORDER BY filename, version DESC",
        user_id,
    )
    files = [_row_to_file(r) for r in rows]
    files.sort(key=lambda f: f.created_at, reverse=True)
    return files


async def get_by_id(
    conn: asyncpg.Connection, user_id: UUID, file_id: UUID
) -> WorkspaceFile | None:
    row = await conn.fetchrow(
        "SELECT id, user_id, task_id, source::text AS source, filename,"
        "       mime_type, storage_url, size_bytes, version, supersedes_id,"
        "       sha256, created_at"
        "  FROM workspace_files"
        " WHERE id = $1 AND user_id = $2",
        file_id,
        user_id,
    )
    return _row_to_file(row) if row else None


async def get_latest_by_filename(
    conn: asyncpg.Connection, user_id: UUID, filename: str
) -> WorkspaceFile | None:
    row = await conn.fetchrow(
        "SELECT id, user_id, task_id, source::text AS source, filename,"
        "       mime_type, storage_url, size_bytes, version, supersedes_id,"
        "       sha256, created_at"
        "  FROM workspace_files"
        " WHERE user_id = $1 AND filename = $2"
        " ORDER BY version DESC LIMIT 1",
        user_id,
        filename,
    )
    return _row_to_file(row) if row else None


async def register(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    source: FileSource,
    filename: str,
    storage_url: str,
    size_bytes: int,
    mime_type: str | None = None,
    sha256: str | None = None,
    task_id: UUID | None = None,
    supersedes_id: UUID | None = None,
    version: int = 1,
) -> WorkspaceFile:
    """Insert a new workspace_files row. Caller is responsible for setting
    `version` / `supersedes_id` correctly when overwriting; for new files
    pass version=1 and supersedes_id=None."""
    row = await conn.fetchrow(
        "INSERT INTO workspace_files"
        " (user_id, task_id, source, filename, mime_type, storage_url,"
        "  size_bytes, version, supersedes_id, sha256)"
        " VALUES ($1, $2, $3::file_source, $4, $5, $6, $7, $8, $9, $10)"
        " RETURNING id, user_id, task_id, source::text AS source, filename,"
        "          mime_type, storage_url, size_bytes, version,"
        "          supersedes_id, sha256, created_at",
        user_id, task_id, source, filename, mime_type, storage_url,
        size_bytes, version, supersedes_id, sha256,
    )
    return _row_to_file(row)
