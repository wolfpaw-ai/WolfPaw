"""Storage interface — every backend (local FS, S3, Drive, Dropbox) implements
the same shape so the workspace API and the `read_doc` / `write_doc` tools
don't have to know which backend they're talking to.

The agent never sees raw paths or signed URLs in tool I/O. It deals in
logical filenames; the workspace layer resolves them through Storage.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Mapping
from uuid import UUID


@dataclass(frozen=True)
class SignedURL:
    """Short-lived URL the client should hit directly (PUT for upload,
    GET for download). For LocalStorage this round-trips through
    `/workspace/blob/...`; for S3Storage this is a real AWS presigned URL.

    `headers` carries any required request headers (e.g. content-length
    limit) the client must echo. `max_bytes` is informational — both the
    issuer and the bucket policy enforce it independently.
    """

    url: str
    headers: Mapping[str, str]
    max_bytes: int
    expires_at_unix: int


@dataclass(frozen=True)
class StorageObject:
    """Pointer to a stored object. `storage_url` is opaque to callers — it's
    a backend-specific locator (`local://`, `s3://...`) that the same
    backend can resolve back to bytes."""

    storage_url: str
    size_bytes: int
    sha256: str


class Storage(ABC):
    name: str

    @abstractmethod
    async def put(self, user_id: UUID, filename: str, data: bytes) -> StorageObject:
        """Write `data` to the user's workspace. Overwrites are allowed at
        this layer — the workspace_files versioning rule lives in the DAO,
        not here."""

    @abstractmethod
    async def get(self, user_id: UUID, filename: str) -> bytes:
        """Read bytes for `filename` from the user's workspace.
        Raises FileNotFoundError if missing."""

    @abstractmethod
    async def delete(self, user_id: UUID, filename: str) -> None:
        """Remove an object. No-op if missing."""

    @abstractmethod
    async def exists(self, user_id: UUID, filename: str) -> bool: ...

    @abstractmethod
    def issue_upload_url(
        self,
        user_id: UUID,
        filename: str,
        *,
        max_bytes: int | None = None,
        ttl_seconds: int | None = None,
    ) -> SignedURL:
        """Generate a short-lived URL the client can PUT bytes to."""

    @abstractmethod
    def issue_download_url(
        self,
        user_id: UUID,
        filename: str,
        *,
        ttl_seconds: int | None = None,
    ) -> SignedURL:
        """Generate a short-lived URL the client can GET bytes from."""

    def supports_local_blob_endpoints(self) -> bool:
        """True iff this backend's signed URLs point at this process's
        `/workspace/blob/*` endpoints (LocalStorage). Used by the router to
        decide whether to mount those endpoints."""
        return False
