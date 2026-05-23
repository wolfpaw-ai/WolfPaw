"""Tests for the 4 artifact tools — schema validation + emit_artifact wiring
with a fake Sandbox that captures script + inputs and emits a magic-number
payload back. End-to-end tests with real libs live in
test_tool_artifacts_e2e.py."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from wolfpaw.sandbox import reset_manager
from wolfpaw.sandbox.base import CodeResult, InstallResult, Sandbox
from wolfpaw.toolbox.registry import ToolContext, ToolError, get_registry
from wolfpaw.workspace import files as files_dao
from wolfpaw.workspace.files import WorkspaceCollision


# --- fakes -----------------------------------------------------------------


class FakeSandbox(Sandbox):
    name = "fake"

    def __init__(self, *, output_bytes: bytes = b"\x00fake\x00"):
        self.id = uuid4()
        self.user_id = uuid4()
        self.task_id: UUID | None = None
        self._files: dict[str, bytes] = {}
        self._output_bytes = output_bytes
        self.last_script: str | None = None
        self.last_inputs: dict | None = None
        self.output_basename = "output.xlsx"

    @property
    def elapsed_compute_seconds(self) -> float:
        return 0.0

    async def write_file(self, path: str, data: bytes) -> None:
        self._files[path] = data
        if path == "inputs.json":
            self.last_inputs = json.loads(data.decode("utf-8"))

    async def read_file(self, path: str) -> bytes:
        return self._output_bytes

    async def run_python(self, code, *, timeout_seconds=None):
        self.last_script = code
        # Pretend a file was written at one of the known basenames; the
        # tool's read_file is what actually returns _output_bytes.
        return CodeResult(stdout="", stderr="", exit_code=0, elapsed_seconds=0.01)

    async def install_package(self, package):
        return InstallResult(package=package, ok=True, log="")

    async def close(self):
        pass


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


@asynccontextmanager
async def _fake_acquire():
    yield None


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def artifact_env(tmp_path, monkeypatch):
    """Point storage at tmp, wire fake sandbox + fake DAO, return the fake
    sandbox + the in-memory file list so tests can inspect what happened."""
    from wolfpaw.config import get_settings
    from wolfpaw.storage import reset_storage

    monkeypatch.setenv("WOLFPAW_LOCAL_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("WOLFPAW_WEB_BASE_URL", "http://testserver")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_storage()
    reset_manager()

    sandbox = FakeSandbox()

    class FakeManager:
        async def get(self, _user_id, _task_id=None):
            return sandbox

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
    import wolfpaw.toolbox.tools._artifact as art
    monkeypatch.setattr(art, "files_dao", files_dao)

    # Replace the manager singleton in the artifact helper with a fake.
    import wolfpaw.toolbox.tools._artifact as art_mod
    monkeypatch.setattr(art_mod, "get_manager", lambda: FakeManager())

    # Stub out DB acquire (artifact + sandbox manager both use it).
    monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.toolbox.tools._artifact.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.sandbox.metering.acquire", _fake_acquire)

    yield sandbox, rows

    reset_storage()
    reset_manager()
    get_settings.cache_clear()  # type: ignore[attr-defined]


# --- shared helper happy path ----------------------------------------------


async def test_emit_artifact_writes_workspace_row_and_storage(artifact_env):
    sandbox, rows = artifact_env
    tool = get_registry().get("create_spreadsheet")
    ctx = ToolContext(user_id=uuid4(), task_id=uuid4())
    result = await tool.run(
        ctx,
        filename="report.xlsx",
        sheets=[{"name": "Sales", "rows": [["Date", "Revenue"], ["2026", 100]]}],
    )
    assert result["filename"] == "report.xlsx"
    assert result["version"] == 1
    assert len(rows) == 1
    assert rows[0].source == "agent_output"
    # The fake sandbox got our JSON inputs.
    assert sandbox.last_inputs == {"sheets": [
        {"name": "Sales", "rows": [["Date", "Revenue"], ["2026", 100]]},
    ]}
    # And the script that ran was the spreadsheet template.
    assert "openpyxl" in sandbox.last_script


async def test_collision_raises_workspace_collision(artifact_env):
    _sandbox, rows = artifact_env
    tool = get_registry().get("create_spreadsheet")
    uid = uuid4()
    ctx = ToolContext(user_id=uid)
    await tool.run(
        ctx,
        filename="x.xlsx",
        sheets=[{"name": "S", "rows": [["a"]]}],
    )
    with pytest.raises(WorkspaceCollision):
        await tool.run(
            ctx,
            filename="x.xlsx",
            sheets=[{"name": "S", "rows": [["b"]]}],
        )


async def test_overwrite_true_version_bumps(artifact_env):
    _sandbox, rows = artifact_env
    tool = get_registry().get("create_spreadsheet")
    uid = uuid4()
    ctx = ToolContext(user_id=uid)
    await tool.run(ctx, filename="x.xlsx",
                   sheets=[{"name": "S", "rows": [["a"]]}])
    out = await tool.run(ctx, filename="x.xlsx",
                         sheets=[{"name": "S", "rows": [["b"]]}],
                         overwrite=True)
    assert out["version"] == 2


async def test_script_failure_becomes_tool_error(artifact_env, monkeypatch):
    sandbox, _rows = artifact_env

    async def boom(code, *, timeout_seconds=None):
        return CodeResult(
            stdout="", stderr="boom", exit_code=1, elapsed_seconds=0.0,
        )

    monkeypatch.setattr(sandbox, "run_python", boom)
    tool = get_registry().get("create_spreadsheet")
    ctx = ToolContext(user_id=uuid4())
    with pytest.raises(ToolError, match="script failed"):
        await tool.run(ctx, filename="x.xlsx",
                       sheets=[{"name": "S", "rows": [["a"]]}])


# --- per-tool schema validation --------------------------------------------


async def test_spreadsheet_rejects_wrong_extension(artifact_env):
    tool = get_registry().get("create_spreadsheet")
    ctx = ToolContext(user_id=uuid4())
    with pytest.raises(ToolError, match=".xlsx"):
        await tool.run(ctx, filename="notes.txt",
                       sheets=[{"name": "S", "rows": [["a"]]}])


async def test_chart_requires_known_type(artifact_env):
    tool = get_registry().get("create_chart")
    ctx = ToolContext(user_id=uuid4())
    with pytest.raises(ToolError, match="type"):
        await tool.run(ctx, filename="c.png", type="pie",
                       series=[{"x": [1], "y": [2]}])


async def test_chart_requires_matching_xy_lengths(artifact_env):
    tool = get_registry().get("create_chart")
    ctx = ToolContext(user_id=uuid4())
    with pytest.raises(ToolError, match="equal-length"):
        await tool.run(ctx, filename="c.png", type="line",
                       series=[{"x": [1, 2], "y": [3]}])


async def test_chart_rejects_unknown_extension(artifact_env):
    tool = get_registry().get("create_chart")
    ctx = ToolContext(user_id=uuid4())
    with pytest.raises(ToolError, match=".png or .svg"):
        await tool.run(ctx, filename="c.gif", type="line",
                       series=[{"x": [1], "y": [2]}])


async def test_slides_rejects_wrong_extension(artifact_env):
    tool = get_registry().get("create_slides")
    ctx = ToolContext(user_id=uuid4())
    with pytest.raises(ToolError, match=".pptx"):
        await tool.run(ctx, filename="deck.pdf",
                       slides=[{"title": "x"}])


async def test_pdf_rejects_empty_html(artifact_env):
    tool = get_registry().get("create_pdf")
    ctx = ToolContext(user_id=uuid4())
    with pytest.raises(ToolError, match="html"):
        await tool.run(ctx, filename="x.pdf", html="   ")
