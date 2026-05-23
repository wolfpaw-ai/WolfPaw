"""S3Storage: bytes in S3 under `s3://<bucket>/<user_id_hex>/<filename>`.

Hosted-only. The boto3 dep is gated behind the `[s3]` extra; importing
this module won't pull boto3 in unless an S3Storage instance is actually
constructed.

The presigned URLs returned here are real AWS URLs — bytes never transit
the API host, which matches the plan's "frontend ↔ S3 direct" promise.
The bucket policy must deny writes/reads that cross the per-user prefix;
we still server-side-validate the requested key starts with
`<authenticated_user_id_hex>/` before issuing a signature.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any
from uuid import UUID

from wolfpaw.storage.base import SignedURL, Storage, StorageObject
from wolfpaw.storage.local import _validate_filename


class S3Storage(Storage):
    name = "s3"

    def __init__(
        self,
        *,
        bucket: str,
        region: str,
        default_ttl_seconds: int,
        default_max_bytes: int,
        client: Any | None = None,
    ) -> None:
        if not bucket:
            raise ValueError(
                "S3Storage requires WOLFPAW_S3_BUCKET to be set."
            )
        self._bucket = bucket
        self._region = region
        self._default_ttl = default_ttl_seconds
        self._default_max = default_max_bytes
        self._client = client or self._build_client()

    @staticmethod
    def _build_client() -> Any:
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as e:
            raise ImportError(
                "boto3 is required for S3Storage. Install with"
                " `pip install wolfpaw[s3]`."
            ) from e
        return boto3.client("s3")

    def _key_for(self, user_id: UUID, filename: str) -> str:
        _validate_filename(filename)
        return f"{user_id.hex}/{filename}"

    # --- Storage methods -----------------------------------------------------

    async def put(
        self, user_id: UUID, filename: str, data: bytes
    ) -> StorageObject:
        key = self._key_for(user_id, filename)

        def _put() -> StorageObject:
            self._client.put_object(Bucket=self._bucket, Key=key, Body=data)
            return StorageObject(
                storage_url=f"s3://{self._bucket}/{key}",
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )

        return await asyncio.to_thread(_put)

    async def get(self, user_id: UUID, filename: str) -> bytes:
        key = self._key_for(user_id, filename)

        def _get() -> bytes:
            try:
                resp = self._client.get_object(Bucket=self._bucket, Key=key)
            except self._client.exceptions.NoSuchKey as e:
                raise FileNotFoundError(filename) from e
            return resp["Body"].read()

        return await asyncio.to_thread(_get)

    async def delete(self, user_id: UUID, filename: str) -> None:
        key = self._key_for(user_id, filename)
        await asyncio.to_thread(
            lambda: self._client.delete_object(Bucket=self._bucket, Key=key)
        )

    async def exists(self, user_id: UUID, filename: str) -> bool:
        key = self._key_for(user_id, filename)

        def _head() -> bool:
            try:
                self._client.head_object(Bucket=self._bucket, Key=key)
                return True
            except Exception:
                return False

        return await asyncio.to_thread(_head)

    # --- signed URLs ---------------------------------------------------------

    def issue_upload_url(
        self,
        user_id: UUID,
        filename: str,
        *,
        max_bytes: int | None = None,
        ttl_seconds: int | None = None,
    ) -> SignedURL:
        key = self._key_for(user_id, filename)
        max_b = max_bytes if max_bytes is not None else self._default_max
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        url = self._client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=ttl,
            HttpMethod="PUT",
        )
        return SignedURL(
            url=url,
            headers={"content-type": "application/octet-stream"},
            max_bytes=max_b,
            expires_at_unix=int(time.time()) + ttl,
        )

    def issue_download_url(
        self,
        user_id: UUID,
        filename: str,
        *,
        ttl_seconds: int | None = None,
    ) -> SignedURL:
        key = self._key_for(user_id, filename)
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        url = self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=ttl,
        )
        return SignedURL(
            url=url, headers={}, max_bytes=0,
            expires_at_unix=int(time.time()) + ttl,
        )
