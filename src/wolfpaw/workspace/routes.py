"""Workspace HTTP API:
    GET  /workspace/files                       — list this user's latest files
    POST /workspace/upload-url                  — mint a short-lived upload URL
    POST /workspace/files                       — register a completed upload
    GET  /workspace/files/{id}/download-url     — mint a short-lived download URL
    PUT  /workspace/blob/upload?token=...       — LocalStorage byte sink
    GET  /workspace/blob/download?token=...     — LocalStorage byte source

The blob routes only do useful work for LocalStorage; under S3Storage the
signed URLs point at AWS and these endpoints are unused. The router still
mounts them so swapping backends is a config change, not a code change.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from starlette.responses import Response

from wolfpaw.auth.deps import require_user_id
from wolfpaw.memory.db import acquire
from wolfpaw.storage import get_storage
from wolfpaw.storage.signing import TokenError, verify_token
from wolfpaw.workspace import files as files_dao

router = APIRouter(prefix="/workspace", tags=["workspace"])


# --- request/response shapes -------------------------------------------------


class UploadURLRequest(BaseModel):
    filename: str
    max_bytes: int | None = None


class UploadURLResponse(BaseModel):
    url: str
    headers: dict[str, str]
    max_bytes: int
    expires_at_unix: int


class RegisterFileRequest(BaseModel):
    filename: str
    size_bytes: int
    mime_type: str | None = None
    sha256: str | None = None


class FileResponse(BaseModel):
    id: str
    filename: str
    mime_type: str | None
    size_bytes: int
    version: int
    source: str
    sha256: str | None
    created_at: str

    @classmethod
    def from_dao(cls, f: files_dao.WorkspaceFile) -> "FileResponse":
        return cls(
            id=str(f.id),
            filename=f.filename,
            mime_type=f.mime_type,
            size_bytes=f.size_bytes,
            version=f.version,
            source=f.source,
            sha256=f.sha256,
            created_at=f.created_at.isoformat(),
        )


class DownloadURLResponse(BaseModel):
    url: str
    expires_at_unix: int


# --- file management routes --------------------------------------------------


@router.get("/files")
async def list_files(
    user_id: UUID = Depends(require_user_id),
) -> dict[str, list[FileResponse]]:
    async with acquire() as conn:
        files = await files_dao.list_latest(conn, user_id)
    return {"files": [FileResponse.from_dao(f) for f in files]}


@router.post("/upload-url", response_model=UploadURLResponse)
async def upload_url(
    payload: UploadURLRequest,
    user_id: UUID = Depends(require_user_id),
) -> UploadURLResponse:
    try:
        signed = get_storage().issue_upload_url(
            user_id, payload.filename, max_bytes=payload.max_bytes
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return UploadURLResponse(
        url=signed.url,
        headers=dict(signed.headers),
        max_bytes=signed.max_bytes,
        expires_at_unix=signed.expires_at_unix,
    )


@router.post("/files", response_model=FileResponse, status_code=201)
async def register_file(
    payload: RegisterFileRequest,
    user_id: UUID = Depends(require_user_id),
) -> FileResponse:
    """Register an upload that already landed in storage.

    On collision, inserts a new versioned row (supersedes_id points at the
    prior latest). This is the user-driven overwrite path — `write_doc`
    uses a different policy (collisions raise so the executor can ping
    the user for confirmation).
    """
    storage = get_storage()
    if not await storage.exists(user_id, payload.filename):
        raise HTTPException(
            409, f"no bytes found in storage for {payload.filename!r}"
        )
    storage_url = f"{storage.name}://{user_id.hex}/{payload.filename}"
    async with acquire() as conn:
        latest = await files_dao.get_latest_by_filename(
            conn, user_id, payload.filename
        )
        version = (latest.version + 1) if latest else 1
        supersedes_id = latest.id if latest else None
        f = await files_dao.register(
            conn,
            user_id=user_id,
            source="user_upload",
            filename=payload.filename,
            storage_url=storage_url,
            size_bytes=payload.size_bytes,
            mime_type=payload.mime_type,
            sha256=payload.sha256,
            version=version,
            supersedes_id=supersedes_id,
        )
    return FileResponse.from_dao(f)


@router.get("/files/{file_id}/download-url", response_model=DownloadURLResponse)
async def download_url(
    file_id: UUID,
    user_id: UUID = Depends(require_user_id),
) -> DownloadURLResponse:
    async with acquire() as conn:
        f = await files_dao.get_by_id(conn, user_id, file_id)
    if f is None:
        raise HTTPException(404, "file not found")
    signed = get_storage().issue_download_url(user_id, f.filename)
    return DownloadURLResponse(
        url=signed.url, expires_at_unix=signed.expires_at_unix
    )


# --- blob transit (LocalStorage only) ----------------------------------------


def _verify_blob_token(token: str, expected_op: str):
    from wolfpaw.config import get_settings

    settings = get_settings()
    try:
        payload = verify_token(token, secret=settings.secret_key.encode())
    except TokenError as e:
        raise HTTPException(403, f"invalid token: {e}")
    if payload.op != expected_op:
        raise HTTPException(403, f"token op mismatch: got {payload.op!r}")
    return payload


@router.put("/blob/upload")
async def blob_upload(
    request: Request,
    token: str = Query(...),
) -> Response:
    payload = _verify_blob_token(token, "upload")
    body = await request.body()
    if payload.max_bytes and len(body) > payload.max_bytes:
        raise HTTPException(413, "upload exceeds max_bytes")
    storage = get_storage()
    await storage.put(payload.user_id, payload.filename, body)
    return Response(status_code=204)


@router.get("/blob/download")
async def blob_download(token: str = Query(...)) -> Response:
    payload = _verify_blob_token(token, "download")
    storage = get_storage()
    try:
        data = await storage.get(payload.user_id, payload.filename)
    except FileNotFoundError:
        raise HTTPException(404, "file not found")
    return Response(content=data, media_type="application/octet-stream")
