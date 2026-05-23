"""Pure-unit tests for the HMAC-signed token format used by LocalStorage's
pseudo-presigned URLs."""

from __future__ import annotations

import time
from uuid import uuid4

import pytest

from wolfpaw.storage.signing import (
    TokenError,
    TokenPayload,
    mint_token,
    verify_token,
)


SECRET = b"unit-test-secret"


def _payload(op: str = "upload", filename: str = "a.txt", ttl: int = 300) -> TokenPayload:
    return TokenPayload(
        user_id=uuid4(),
        filename=filename,
        op=op,  # type: ignore[arg-type]
        max_bytes=1024,
        exp=int(time.time()) + ttl,
    )


def test_round_trip():
    p = _payload()
    token = mint_token(p, secret=SECRET)
    decoded = verify_token(token, secret=SECRET)
    assert decoded == p


def test_bad_signature_rejected():
    p = _payload()
    token = mint_token(p, secret=SECRET)
    with pytest.raises(TokenError):
        verify_token(token, secret=b"different-secret")


def test_expired_token_rejected():
    p = _payload(ttl=-10)
    token = mint_token(p, secret=SECRET)
    with pytest.raises(TokenError, match="expired"):
        verify_token(token, secret=SECRET)


def test_malformed_token_rejected():
    with pytest.raises(TokenError):
        verify_token("not-a-token", secret=SECRET)
    with pytest.raises(TokenError):
        verify_token("aaaa.bbbb.cccc", secret=SECRET)


def test_tampered_payload_rejected():
    p = _payload()
    token = mint_token(p, secret=SECRET)
    body, sig = token.split(".")
    # Flip a bit in the body — the cached signature won't match.
    tampered = body[:-1] + ("A" if body[-1] != "A" else "B") + "." + sig
    with pytest.raises(TokenError):
        verify_token(tampered, secret=SECRET)
