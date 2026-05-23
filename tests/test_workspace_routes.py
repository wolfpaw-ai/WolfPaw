"""End-to-end HTTP tests for the workspace API against LocalStorage.

These exercise the full upload-url → PUT → register → list → download-url → GET
cycle in-process. Auth is bypassed via dependency override; storage is
pointed at tmp_path; the DAO is monkeypatched to in-memory dicts so tests
don't need a Postgres."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from wolfpaw.api import create_app
from wolfpaw.auth.deps import require_user_id
from wolfpaw.workspace import files as files_dao


@asynccontextmanager
async def _fake_acquire():
    yield None


def _patch_acquire(monkeypatch) -> None:
    """Stub out the asyncpg pool so DAO-patched tests don't need a real DB."""
    monkeypatch.setattr("wolfpaw.memory.db.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.workspace.routes.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.toolbox.tools.read_doc.acquire", _fake_acquire)
    monkeypatch.setattr("wolfpaw.toolbox.tools.write_doc.acquire", _fake_acquire)


@pytest.fixture
def tmp_storage(tmp_path, monkeypatch):
    """Point storage at tmp_path. Patches WOLFPAW_LOCAL_STORAGE_ROOT and
    resets the storage singleton so we get a fresh LocalStorage."""
    monkeypatch.setenv("WOLFPAW_LOCAL_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("WOLFPAW_WEB_BASE_URL", "http://testserver")
    from wolfpaw.config import get_settings
    from wolfpaw.storage import reset_storage

    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_storage()
    yield tmp_path
    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_storage()


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


@pytest.fixture
def in_memory_files(monkeypatch):
    """Patch the workspace_files DAO to use an in-memory list. Lets HTTP
    tests run without a Postgres."""
    _patch_acquire(monkeypatch)
    rows: list[_Row] = []

    async def list_latest(_conn, user_id):
        latest: dict[str, _Row] = {}
        for r in rows:
            if r.user_id != user_id:
                continue
            cur = latest.get(r.filename)
            if cur is None or r.version > cur.version:
                latest[r.filename] = r
        out = sorted(latest.values(), key=lambda r: r.created_at, reverse=True)
        return [_dao_from_row(r) for r in out]

    async def get_by_id(_conn, user_id, file_id):
        for r in rows:
            if r.id == file_id and r.user_id == user_id:
                return _dao_from_row(r)
        return None

    async def get_latest_by_filename(_conn, user_id, filename):
        best: _Row | None = None
        for r in rows:
            if r.user_id != user_id or r.filename != filename:
                continue
            if best is None or r.version > best.version:
                best = r
        return _dao_from_row(best) if best else None

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
        return _dao_from_row(r)

    monkeypatch.setattr(files_dao, "list_latest", list_latest)
    monkeypatch.setattr(files_dao, "get_by_id", get_by_id)
    monkeypatch.setattr(files_dao, "get_latest_by_filename", get_latest_by_filename)
    monkeypatch.setattr(files_dao, "register", register)

    # Patch the names imported into the routes module too (they're rebound
    # via `from wolfpaw.workspace import files as files_dao`, so monkeypatching
    # the package attribute is enough).
    import wolfpaw.workspace.routes as routes_mod
    monkeypatch.setattr(routes_mod, "files_dao", files_dao)

    # And the tool modules.
    import wolfpaw.toolbox.tools.read_doc as rd_mod
    import wolfpaw.toolbox.tools.write_doc as wd_mod
    monkeypatch.setattr(rd_mod, "files_dao", files_dao)
    monkeypatch.setattr(wd_mod, "files_dao", files_dao)

    yield rows


def _dao_from_row(r: _Row) -> files_dao.WorkspaceFile:
    return files_dao.WorkspaceFile(
        id=r.id, user_id=r.user_id, task_id=r.task_id, source=r.source,  # type: ignore[arg-type]
        filename=r.filename, mime_type=r.mime_type, storage_url=r.storage_url,
        size_bytes=r.size_bytes, version=r.version, supersedes_id=r.supersedes_id,
        sha256=r.sha256, created_at=r.created_at,
    )


def _client(uid: UUID) -> TestClient:
    app = create_app()
    app.dependency_overrides[require_user_id] = lambda: uid
    return TestClient(app)


# --- tests ------------------------------------------------------------------


def test_upload_register_list_download_round_trip(tmp_storage, in_memory_files):
    uid = uuid4()
    client = _client(uid)

    # 1) Mint an upload URL.
    r = client.post("/workspace/upload-url", json={"filename": "note.md"})
    assert r.status_code == 200, r.text
    url = r.json()["url"]

    # 2) PUT bytes to that URL (routes back into the same app).
    path = urlparse(url).path + "?" + urlparse(url).query
    r = client.put(path, content=b"hello workspace")
    assert r.status_code == 204, r.text

    # 3) Register the upload.
    r = client.post(
        "/workspace/files",
        json={"filename": "note.md", "size_bytes": 15, "mime_type": "text/markdown"},
    )
    assert r.status_code == 201, r.text
    file_id = r.json()["id"]
    assert r.json()["version"] == 1

    # 4) List should show it.
    r = client.get("/workspace/files")
    assert r.status_code == 200
    names = [f["filename"] for f in r.json()["files"]]
    assert names == ["note.md"]

    # 5) Mint a download URL and GET it.
    r = client.get(f"/workspace/files/{file_id}/download-url")
    assert r.status_code == 200, r.text
    dl_url = r.json()["url"]
    dl_path = urlparse(dl_url).path + "?" + urlparse(dl_url).query
    r = client.get(dl_path)
    assert r.status_code == 200
    assert r.content == b"hello workspace"


def test_register_versions_on_collision(tmp_storage, in_memory_files):
    uid = uuid4()
    client = _client(uid)
    # First write.
    r = client.post("/workspace/upload-url", json={"filename": "x.md"})
    url = urlparse(r.json()["url"]).path + "?" + urlparse(r.json()["url"]).query
    client.put(url, content=b"v1")
    r1 = client.post("/workspace/files", json={"filename": "x.md", "size_bytes": 2})
    assert r1.json()["version"] == 1

    # Second write with same name → version 2.
    r = client.post("/workspace/upload-url", json={"filename": "x.md"})
    url = urlparse(r.json()["url"]).path + "?" + urlparse(r.json()["url"]).query
    client.put(url, content=b"v2-bytes")
    r2 = client.post("/workspace/files", json={"filename": "x.md", "size_bytes": 8})
    assert r2.status_code == 201
    assert r2.json()["version"] == 2

    # Listing should show only the v2 row.
    r = client.get("/workspace/files")
    files = r.json()["files"]
    assert len(files) == 1
    assert files[0]["version"] == 2


def test_upload_rejects_path_traversal(tmp_storage, in_memory_files):
    uid = uuid4()
    client = _client(uid)
    r = client.post("/workspace/upload-url", json={"filename": "../escape"})
    assert r.status_code == 400


def test_blob_upload_rejects_unsigned_token(tmp_storage, in_memory_files):
    uid = uuid4()
    client = _client(uid)
    r = client.put("/workspace/blob/upload?token=garbage", content=b"x")
    assert r.status_code == 403


def test_register_without_bytes_409s(tmp_storage, in_memory_files):
    uid = uuid4()
    client = _client(uid)
    r = client.post(
        "/workspace/files",
        json={"filename": "never-uploaded.md", "size_bytes": 1},
    )
    assert r.status_code == 409


def test_download_url_requires_owning_user(tmp_storage, in_memory_files):
    """A file owned by user A is invisible to user B."""
    a, b = uuid4(), uuid4()
    client_a = _client(a)
    r = client_a.post("/workspace/upload-url", json={"filename": "secret.md"})
    url = urlparse(r.json()["url"]).path + "?" + urlparse(r.json()["url"]).query
    client_a.put(url, content=b"private")
    r = client_a.post("/workspace/files", json={"filename": "secret.md", "size_bytes": 7})
    file_id = r.json()["id"]

    client_b = _client(b)
    r = client_b.get(f"/workspace/files/{file_id}/download-url")
    assert r.status_code == 404


def test_list_requires_auth(tmp_storage):
    # No dependency override — should 401.
    app = create_app()
    client = TestClient(app)
    r = client.get("/workspace/files")
    assert r.status_code == 401
