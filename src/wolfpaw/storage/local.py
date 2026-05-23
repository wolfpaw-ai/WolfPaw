"""LocalStorage: bytes on disk under `<root>/<user_id_hex>/<filename>`.

Self-host default. Also fine for dev / tests. Mounts blob-transit
endpoints (`/workspace/blob/upload`, `/workspace/blob/download`) that
the signed URLs point at.

Filename validation rejects anything that would escape the user's
prefix (`..`, leading `/`, NUL bytes, etc) — defense in depth on top
of the joined-path check.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from pathlib import Path
from uuid import UUID

from wolfpaw.storage.base import SignedURL, Storage, StorageObject
from wolfpaw.storage.signing import TokenPayload, mint_token

_VALID_FILENAME = re.compile(r"^[A-Za-z0-9._-][A-Za-z0-9 ._-]{0,254}$")


def _validate_filename(filename: str) -> str:
    """Reject anything that could escape the per-user prefix. Returns the
    validated filename unchanged on success."""
    if not filename or filename in (".", ".."):
        raise ValueError("filename is empty or reserved")
    if "/" in filename or "\\" in filename or "\x00" in filename:
        raise ValueError("filename must not contain path separators or NUL")
    if not _VALID_FILENAME.match(filename):
        raise ValueError("filename contains disallowed characters")
    return filename


class LocalStorage(Storage):
    name = "local"

    def __init__(
        self,
        *,
        root: str,
        base_url: str,
        secret: bytes,
        default_ttl_seconds: int,
        default_max_bytes: int,
    ) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._base_url = base_url.rstrip("/")
        self._secret = secret
        self._default_ttl = default_ttl_seconds
        self._default_max = default_max_bytes

    # --- path helpers --------------------------------------------------------

    def _user_dir(self, user_id: UUID) -> Path:
        return self._root / user_id.hex

    def _path_for(self, user_id: UUID, filename: str) -> Path:
        _validate_filename(filename)
        return self._user_dir(user_id) / filename

    # --- Storage methods -----------------------------------------------------

    async def put(
        self, user_id: UUID, filename: str, data: bytes
    ) -> StorageObject:
        path = self._path_for(user_id, filename)

        def _write() -> StorageObject:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return StorageObject(
                storage_url=f"local://{user_id.hex}/{filename}",
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )

        return await asyncio.to_thread(_write)

    async def get(self, user_id: UUID, filename: str) -> bytes:
        path = self._path_for(user_id, filename)

        def _read() -> bytes:
            if not path.exists():
                raise FileNotFoundError(filename)
            return path.read_bytes()

        return await asyncio.to_thread(_read)

    async def delete(self, user_id: UUID, filename: str) -> None:
        path = self._path_for(user_id, filename)

        def _rm() -> None:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

        await asyncio.to_thread(_rm)

    async def exists(self, user_id: UUID, filename: str) -> bool:
        path = self._path_for(user_id, filename)
        return await asyncio.to_thread(path.exists)

    # --- signed URLs ---------------------------------------------------------

    def _make_url(self, op: str, token: str) -> str:
        return f"{self._base_url}/workspace/blob/{op}?token={token}"

    def issue_upload_url(
        self,
        user_id: UUID,
        filename: str,
        *,
        max_bytes: int | None = None,
        ttl_seconds: int | None = None,
    ) -> SignedURL:
        _validate_filename(filename)
        max_b = max_bytes if max_bytes is not None else self._default_max
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        exp = int(time.time()) + ttl
        token = mint_token(
            TokenPayload(
                user_id=user_id, filename=filename, op="upload",
                max_bytes=max_b, exp=exp,
            ),
            secret=self._secret,
        )
        return SignedURL(
            url=self._make_url("upload", token),
            headers={"content-type": "application/octet-stream"},
            max_bytes=max_b,
            expires_at_unix=exp,
        )

    def issue_download_url(
        self,
        user_id: UUID,
        filename: str,
        *,
        ttl_seconds: int | None = None,
    ) -> SignedURL:
        _validate_filename(filename)
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        exp = int(time.time()) + ttl
        token = mint_token(
            TokenPayload(
                user_id=user_id, filename=filename, op="download",
                max_bytes=0, exp=exp,
            ),
            secret=self._secret,
        )
        return SignedURL(
            url=self._make_url("download", token),
            headers={},
            max_bytes=0,
            expires_at_unix=exp,
        )

    def supports_local_blob_endpoints(self) -> bool:
        return True
