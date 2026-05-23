"""End-to-end tests: real SubprocessSandbox + real libraries, verify the
output bytes have the right magic-number signature for each format.

Skips the PDF test cleanly when weasyprint isn't importable (needs system
libs — Pango/Cairo — that may not be present in CI / local dev)."""

from __future__ import annotations

import importlib.util
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from wolfpaw.sandbox import get_manager, reset_manager
from wolfpaw.toolbox.registry import ToolContext, get_registry
from wolfpaw.workspace import files as files_dao


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


@pytest.fixture
async def real_sandbox_env(tmp_path, monkeypatch):
    """Real SubprocessSandbox + LocalStorage + in-memory DAO. The host venv's
    openpyxl/matplotlib/python-pptx are visible to the child python because
    SubprocessSandbox spawns sys.executable."""
    from wolfpaw.config import get_settings
    from wolfpaw.storage import reset_storage

    monkeypatch.setenv("WOLFPAW_LOCAL_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("WOLFPAW_SANDBOX_BACKEND", "subprocess")
    monkeypatch.setenv("WOLFPAW_WEB_BASE_URL", "http://testserver")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_storage()
    reset_manager()

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
    monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.toolbox.tools._artifact.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.sandbox.metering.acquire", _fake_acquire)

    yield tmp_path, rows

    # Shut down any sandboxes the test spun up before tearing down env.
    await get_manager().shutdown_all()
    reset_manager()
    reset_storage()
    get_settings.cache_clear()  # type: ignore[attr-defined]


def _stored_bytes(rows: list[_Row], tmp_path) -> bytes:
    """Read the bytes the tool wrote to LocalStorage."""
    assert len(rows) == 1
    r = rows[0]
    path = tmp_path / r.user_id.hex / r.filename
    return path.read_bytes()


# --- spreadsheet ------------------------------------------------------------


async def test_create_spreadsheet_emits_valid_xlsx(real_sandbox_env):
    tmp_path, rows = real_sandbox_env
    tool = get_registry().get("create_spreadsheet")
    ctx = ToolContext(user_id=uuid4(), task_id=uuid4())
    out = await tool.run(
        ctx,
        filename="report.xlsx",
        sheets=[{"name": "Sales", "rows": [["Date", "Revenue"], ["2026-01", 1200]]}],
    )
    assert out["filename"] == "report.xlsx"
    data = _stored_bytes(rows, tmp_path)
    # .xlsx is a zip file — magic bytes "PK\x03\x04"
    assert data[:4] == b"PK\x03\x04"
    # Confirm openpyxl can read it back.
    import io
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["Sales"]
    assert wb["Sales"]["A1"].value == "Date"
    assert wb["Sales"]["B2"].value == 1200


# --- chart ------------------------------------------------------------------


async def test_create_chart_emits_png(real_sandbox_env):
    tmp_path, rows = real_sandbox_env
    tool = get_registry().get("create_chart")
    ctx = ToolContext(user_id=uuid4(), task_id=uuid4())
    await tool.run(
        ctx,
        filename="growth.png",
        type="line",
        title="Growth",
        series=[{"name": "rev", "x": [1, 2, 3], "y": [10, 20, 35]}],
    )
    data = _stored_bytes(rows, tmp_path)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


async def test_create_chart_emits_svg(real_sandbox_env):
    tmp_path, rows = real_sandbox_env
    tool = get_registry().get("create_chart")
    ctx = ToolContext(user_id=uuid4(), task_id=uuid4())
    await tool.run(
        ctx, filename="bars.svg", type="bar",
        series=[{"x": ["a", "b"], "y": [1, 2]}],
    )
    data = _stored_bytes(rows, tmp_path)
    assert b"<svg" in data[:500]


# --- slides -----------------------------------------------------------------


async def test_create_slides_emits_valid_pptx(real_sandbox_env):
    tmp_path, rows = real_sandbox_env
    tool = get_registry().get("create_slides")
    ctx = ToolContext(user_id=uuid4(), task_id=uuid4())
    await tool.run(
        ctx,
        filename="pitch.pptx",
        slides=[
            {"title": "Hello", "bullets": ["one", "two"]},
            {"title": "Details", "body": "freeform"},
        ],
    )
    data = _stored_bytes(rows, tmp_path)
    # .pptx is a zip — same PK magic as xlsx.
    assert data[:4] == b"PK\x03\x04"
    import io
    from pptx import Presentation
    prs = Presentation(io.BytesIO(data))
    assert len(prs.slides) == 2


# --- pdf (gated on weasyprint) ---------------------------------------------


@pytest.mark.skipif(
    importlib.util.find_spec("weasyprint") is None,
    reason="weasyprint not installed (system deps required); pip install wolfpaw[pdf]",
)
async def test_create_pdf_emits_valid_pdf(real_sandbox_env):
    tmp_path, rows = real_sandbox_env
    tool = get_registry().get("create_pdf")
    ctx = ToolContext(user_id=uuid4(), task_id=uuid4())
    await tool.run(
        ctx,
        filename="doc.pdf",
        html="<html><body><h1>Hello</h1><p>World.</p></body></html>",
    )
    data = _stored_bytes(rows, tmp_path)
    assert data[:4] == b"%PDF"
