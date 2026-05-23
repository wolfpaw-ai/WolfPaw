"""HMAC-signed token format for LocalStorage's pseudo-presigned URLs.

The signed token round-trips through `/workspace/blob/upload` and
`/workspace/blob/download` — those endpoints validate the signature and
enforce the constraints encoded in the payload (user, filename, op,
max_bytes, expiry). The bytes still transit the API process, which is
fine for a single-process self-host deployment; hosted swaps in
S3Storage and gets real presigned URLs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

Op = Literal["upload", "download"]


@dataclass(frozen=True)
class TokenPayload:
    user_id: UUID
    filename: str
    op: Op
    max_bytes: int
    exp: int  # unix seconds


class TokenError(ValueError):
    """Signed token failed validation: bad signature, expired, malformed."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def mint_token(payload: TokenPayload, *, secret: bytes) -> str:
    body = json.dumps(
        {
            "u": str(payload.user_id),
            "f": payload.filename,
            "op": payload.op,
            "max": payload.max_bytes,
            "exp": payload.exp,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    sig = hmac.new(secret, body, hashlib.sha256).digest()
    return f"{_b64(body)}.{_b64(sig)}"


def verify_token(token: str, *, secret: bytes, now: int | None = None) -> TokenPayload:
    parts = token.split(".")
    if len(parts) != 2:
        raise TokenError("malformed token")
    body_b64, sig_b64 = parts
    try:
        body = _unb64(body_b64)
        sig = _unb64(sig_b64)
    except Exception as e:  # noqa: BLE001 — turn any decode error into TokenError
        raise TokenError("malformed token") from e
    expected = hmac.new(secret, body, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, sig):
        raise TokenError("bad signature")
    try:
        obj = json.loads(body)
        payload = TokenPayload(
            user_id=UUID(obj["u"]),
            filename=obj["f"],
            op=obj["op"],
            max_bytes=int(obj["max"]),
            exp=int(obj["exp"]),
        )
    except Exception as e:  # noqa: BLE001
        raise TokenError("malformed payload") from e
    if payload.op not in ("upload", "download"):
        raise TokenError(f"unknown op {payload.op!r}")
    if (now or int(time.time())) >= payload.exp:
        raise TokenError("token expired")
    return payload
