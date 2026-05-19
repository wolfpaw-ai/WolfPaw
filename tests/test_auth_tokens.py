"""Pure-unit tests for the auth token helpers — no DB needed."""

import time
from uuid import uuid4

from wolfpaw.auth.tokens import (
    hash_magic_link_token,
    mint_session_token,
    new_magic_link_token,
    verify_session_token,
)

SECRET = b"test-secret"


def test_magic_link_token_roundtrip_hashes_deterministically():
    plain, hashed = new_magic_link_token()
    assert len(plain) >= 32
    assert len(hashed) == 64  # sha256 hex
    assert hash_magic_link_token(plain) == hashed
    # New tokens are unique.
    plain2, hashed2 = new_magic_link_token()
    assert plain != plain2
    assert hashed != hashed2


def test_session_token_roundtrip():
    user_id = uuid4()
    token = mint_session_token(user_id=user_id, ttl_seconds=60, secret=SECRET)
    payload = verify_session_token(token, secret=SECRET)
    assert payload is not None
    assert payload.user_id == user_id
    assert payload.expires_at > payload.issued_at


def test_session_token_rejects_bad_signature():
    user_id = uuid4()
    token = mint_session_token(user_id=user_id, ttl_seconds=60, secret=SECRET)
    assert verify_session_token(token, secret=b"wrong-secret") is None


def test_session_token_rejects_tampered_payload():
    user_id = uuid4()
    token = mint_session_token(user_id=user_id, ttl_seconds=60, secret=SECRET)
    body, sig = token.split(".", 1)
    # Flip a char in the body — sig won't match.
    tampered = body[:-1] + ("A" if body[-1] != "A" else "B") + "." + sig
    assert verify_session_token(tampered, secret=SECRET) is None


def test_session_token_rejects_expired():
    user_id = uuid4()
    token = mint_session_token(user_id=user_id, ttl_seconds=-1, secret=SECRET)
    # Sleep tiny bit to ensure exp <= now.
    time.sleep(0.01)
    assert verify_session_token(token, secret=SECRET) is None


def test_session_token_rejects_malformed():
    assert verify_session_token("not.a.token", secret=SECRET) is None
    assert verify_session_token("nodot", secret=SECRET) is None
    assert verify_session_token("", secret=SECRET) is None
