"""LocalStorage adapter tests — bytes on disk + URL minting.

Uses tmp_path so the suite never writes outside the test sandbox.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from wolfpaw.storage.local import LocalStorage, _validate_filename


def _make(tmp_path) -> LocalStorage:
    return LocalStorage(
        root=str(tmp_path),
        base_url="http://localhost:8000",
        secret=b"secret",
        default_ttl_seconds=300,
        default_max_bytes=1024,
    )


async def test_put_then_get_round_trips(tmp_path):
    s = _make(tmp_path)
    uid = uuid4()
    obj = await s.put(uid, "note.md", b"hello world")
    assert obj.size_bytes == 11
    assert obj.sha256 == (
        "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    )
    assert await s.get(uid, "note.md") == b"hello world"
    assert await s.exists(uid, "note.md") is True


async def test_get_missing_raises(tmp_path):
    s = _make(tmp_path)
    with pytest.raises(FileNotFoundError):
        await s.get(uuid4(), "ghost.md")


async def test_delete_then_exists_false(tmp_path):
    s = _make(tmp_path)
    uid = uuid4()
    await s.put(uid, "a.txt", b"x")
    await s.delete(uid, "a.txt")
    assert await s.exists(uid, "a.txt") is False
    # Idempotent — deleting a missing file is fine.
    await s.delete(uid, "a.txt")


async def test_per_user_isolation_on_disk(tmp_path):
    s = _make(tmp_path)
    a, b = uuid4(), uuid4()
    await s.put(a, "shared.md", b"alice")
    await s.put(b, "shared.md", b"bob")
    assert await s.get(a, "shared.md") == b"alice"
    assert await s.get(b, "shared.md") == b"bob"


def test_path_traversal_rejected(tmp_path):
    for bad in ["../etc/passwd", "/abs/path", "foo/bar", "\x00null", "..", "."]:
        with pytest.raises(ValueError):
            _validate_filename(bad)


def test_valid_filenames_accepted():
    for ok in ["report.md", "Q1 2026.xlsx", "data-v2.csv", "_internal.txt", "a"]:
        assert _validate_filename(ok) == ok


def test_upload_url_round_trip_through_blob_endpoint(tmp_path):
    s = _make(tmp_path)
    uid = uuid4()
    signed = s.issue_upload_url(uid, "a.txt")
    assert signed.url.startswith("http://localhost:8000/workspace/blob/upload?token=")
    assert signed.max_bytes == 1024
    assert "content-type" in signed.headers


def test_download_url_has_no_max_bytes(tmp_path):
    s = _make(tmp_path)
    signed = s.issue_download_url(uuid4(), "a.txt")
    assert signed.url.startswith("http://localhost:8000/workspace/blob/download?token=")
    assert signed.max_bytes == 0
