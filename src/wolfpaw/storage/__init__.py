"""Storage abstraction for per-user workspaces.

Backends:
    local  — bytes on disk under `local_storage_root` (self-host default,
             also fine for dev). Pre-signed URLs are HMAC-signed tokens
             that round-trip through `/workspace/blob/{upload,download}`
             endpoints in this same process.
    s3     — bytes in S3 under `s3://<bucket>/<user_id>/...`. Pre-signed
             URLs are real S3 URLs and bytes never transit the API host.
             Requires the optional `[s3]` extra (boto3).

Callers should use `get_storage()` rather than instantiating adapters
directly — the factory honors `settings.storage_backend`.
"""

from __future__ import annotations

from wolfpaw.config import get_settings
from wolfpaw.storage.base import Storage, SignedURL, StorageObject

_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is not None:
        return _storage
    settings = get_settings()
    backend = settings.storage_backend.lower()
    if backend == "local":
        from wolfpaw.storage.local import LocalStorage

        _storage = LocalStorage(
            root=settings.local_storage_root,
            base_url=settings.web_base_url,
            secret=settings.secret_key.encode(),
            default_ttl_seconds=settings.workspace_signed_url_ttl_seconds,
            default_max_bytes=settings.workspace_upload_max_bytes,
        )
    elif backend == "s3":
        from wolfpaw.storage.s3 import S3Storage

        _storage = S3Storage(
            bucket=settings.s3_bucket,
            region=settings.s3_region,
            default_ttl_seconds=settings.workspace_signed_url_ttl_seconds,
            default_max_bytes=settings.workspace_upload_max_bytes,
        )
    else:
        raise ValueError(
            f"Unknown WOLFPAW_STORAGE_BACKEND={backend!r}; expected 'local' or 's3'"
        )
    return _storage


def reset_storage() -> None:
    """Test/dev hook to drop the cached storage backend."""
    global _storage
    _storage = None


__all__ = ["Storage", "SignedURL", "StorageObject", "get_storage", "reset_storage"]
