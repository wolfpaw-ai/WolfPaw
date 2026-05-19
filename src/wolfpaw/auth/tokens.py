"""Token utilities — magic-link tokens (one-time, DB-tracked) and session
tokens (HMAC-signed, stateless).

Session tokens are minimal JWT-shaped strings: `<base64url(json_payload)>.<base64url(hmac_sha256(payload, secret))>`.
Payload: `{"sub": user_id, "iat": int, "exp": int}`. No `alg` field — we
control both producer and consumer; a fixed HS256 is implicit.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Final
from uuid import UUID

MAGIC_LINK_TOKEN_BYTES: Final = 32  # 256 bits


# ---------- magic link tokens ----------


def new_magic_link_token() -> tuple[str, str]:
    """Return (plain_token, sha256_hex_hash). Store the hash; email the plain."""
    plain = secrets.token_urlsafe(MAGIC_LINK_TOKEN_BYTES)
    return plain, hash_magic_link_token(plain)


def hash_magic_link_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------- session tokens ----------


@dataclass(frozen=True, slots=True)
class SessionPayload:
    user_id: UUID
    issued_at: int
    expires_at: int


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def mint_session_token(*, user_id: UUID, ttl_seconds: int, secret: bytes) -> str:
    now = int(time.time())
    payload = {"sub": str(user_id), "iat": now, "exp": now + ttl_seconds}
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    mac = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url(mac)}"


def verify_session_token(token: str, *, secret: bytes) -> SessionPayload | None:
    """Returns the payload on success; None on any failure (bad shape,
    bad signature, expired)."""
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    try:
        provided = _b64url_decode(sig)
    except (ValueError, base64.binascii.Error):
        return None
    if not hmac.compare_digest(expected, provided):
        return None
    try:
        payload = json.loads(_b64url_decode(body))
        user_id = UUID(payload["sub"])
        exp = int(payload["exp"])
        iat = int(payload["iat"])
    except (ValueError, KeyError, TypeError):
        return None
    if exp <= int(time.time()):
        return None
    return SessionPayload(user_id=user_id, issued_at=iat, expires_at=exp)
