"""read_doc / write_doc against LocalStorage with an in-memory DAO patch.

Same DAO-patching trick as the workspace HTTP tests — keeps the v1 doc
tools testable without a Postgres.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from wolfpaw.toolbox.registry import ToolContext, ToolError, get_registry
from wolfpaw.workspace import files as files_dao
from wolfpaw.workspace.files import WorkspaceCollision


@asynccontextmanager
async def _fake_acquire():
    yield None


@dataclass
class _Row:
    id: UUID
    user_id: UUID
    task_id: UUID | None
    source: str
    filename: str
    mime_type: str | None
    storage_url: str
    size_bytes: int
    version: int
    supersedes_id: UUID | None
    sha256: str | None
    created_at: datetime


def _to_dao(r: _Row) -> files_dao.WorkspaceFile:
    return files_dao.WorkspaceFile(
        id=r.id, user_id=r.user_id, task_id=r.task_id, source=r.source,  # type: ignore[arg-type]
        filename=r.filename, mime_type=r.mime_type, storage_url=r.storage_url,
        size_bytes=r.size_bytes, version=r.version, supersedes_id=r.supersedes_id,
        sha256=r.sha256, created_at=r.created_at,
    )


@pytest.fixture
def storage_and_dao(tmp_path, monkeypatch):
    monkeypatch.setenv("WOLFPAW_LOCAL_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("WOLFPAW_WEB_BASE_URL", "http://testserver")
    from wolfpaw.config import get_settings
    from wolfpaw.storage import reset_storage

    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_storage()
    monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.toolbox.tools.read_doc.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.toolbox.tools.write_doc.acquire", _fake_acquire)

    rows: list[_Row] = []

    async def get_latest_by_filename(_conn, user_id, filename):
        best: _Row | None = None
        for r in rows:
            if r.user_id != user_id or r.filename != filename:
                continue
            if best is None or r.version > best.version:
                best = r
        return _to_dao(best) if best else None

    async def register(
        _conn, *, user_id, source, filename, storage_url, size_bytes,
        mime_type=None, sha256=None, task_id=None, supersedes_id=None,
        version=1,
    ):
        r = _Row(
            id=uuid4(), user_id=user_id, task_id=task_id, source=source,
            filename=filename, mime_type=mime_type, storage_url=storage_url,
            size_bytes=size_bytes, version=version, supersedes_id=supersedes_id,
            sha256=sha256, created_at=datetime.now(timezone.utc),
        )
        rows.append(r)
        return _to_dao(r)

    monkeypatch.setattr(files_dao, "get_latest_by_filename", get_latest_by_filename)
    monkeypatch.setattr(files_dao, "register", register)
    import wolfpaw.toolbox.tools.read_doc as rd
    import wolfpaw.toolbox.tools.write_doc as wd
    monkeypatch.setattr(rd, "files_dao", files_dao)
    monkeypatch.setattr(wd, "files_dao", files_dao)

    yield rows

    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_storage()


async def test_write_then_read_round_trips(storage_and_dao):
    uid = uuid4()
    ctx = ToolContext(user_id=uid)
    write = get_registry().get("write_doc")
    read = get_registry().get("read_doc")

    out = await write.run(ctx, filename="note.md", content="# Hello")
    assert out["version"] == 1
    assert out["filename"] == "note.md"

    got = await read.run(ctx, filename="note.md")
    assert got["text"] == "# Hello"
    assert got["version"] == 1


async def test_write_collision_raises_workspace_collision(storage_and_dao):
    uid = uuid4()
    ctx = ToolContext(user_id=uid)
    write = get_registry().get("write_doc")
    await write.run(ctx, filename="x.md", content="v1")
    with pytest.raises(WorkspaceCollision):
        await write.run(ctx, filename="x.md", content="v2")


async def test_write_overwrite_true_bumps_version(storage_and_dao):
    uid = uuid4()
    ctx = ToolContext(user_id=uid)
    write = get_registry().get("write_doc")
    await write.run(ctx, filename="x.md", content="v1")
    out = await write.run(ctx, filename="x.md", content="v2 contents", overwrite=True)
    assert out["version"] == 2
    read = get_registry().get("read_doc")
    got = await read.run(ctx, filename="x.md")
    assert got["text"] == "v2 contents"
    assert got["version"] == 2


async def test_read_missing_raises(storage_and_dao):
    uid = uuid4()
    ctx = ToolContext(user_id=uid)
    read = get_registry().get("read_doc")
    with pytest.raises(ToolError):
        await read.run(ctx, filename="ghost.md")


async def test_read_isolated_per_user(storage_and_dao):
    a, b = uuid4(), uuid4()
    write = get_registry().get("write_doc")
    read = get_registry().get("read_doc")
    await write.run(ToolContext(user_id=a), filename="private.md", content="alice's")
    with pytest.raises(ToolError):
        await read.run(ToolContext(user_id=b), filename="private.md")
