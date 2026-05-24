"""Unit tests for `channels.slack_signing` — HMAC verify + replay guard."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from wolfpaw.channels.slack_signing import (
    MAX_TIMESTAMP_SKEW_SECONDS,
    BadSignature,
    verify,
)


_SECRET = "8f742231b10e8888abcd99yyyzz77789"


def _signed(body: bytes, timestamp: str, secret: str = _SECRET) -> str:
    basestring = b"v0:" + timestamp.encode() + b":" + body
    digest = hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def test_valid_signature_passes():
    body = b'{"type":"event_callback"}'
    ts = "1700000000"
    sig = _signed(body, ts)
    # Pass `now` so the replay window check passes regardless of when
    # the test runs.
    verify(
        signing_secret=_SECRET, timestamp=ts, signature=sig,
        body=body, now=1700000010,
    )


def test_tampered_body_fails():
    ts = "1700000000"
    sig = _signed(b"original", ts)
    with pytest.raises(BadSignature, match="signature mismatch"):
        verify(
            signing_secret=_SECRET, timestamp=ts, signature=sig,
            body=b"TAMPERED", now=1700000010,
        )


def test_wrong_secret_fails():
    body = b'{}'
    ts = "1700000000"
    sig = _signed(body, ts, secret="some-other-secret")
    with pytest.raises(BadSignature):
        verify(
            signing_secret=_SECRET, timestamp=ts, signature=sig,
            body=body, now=1700000010,
        )


def test_replay_outside_window_fails():
    body = b'{}'
    ts = "1700000000"
    sig = _signed(body, ts)
    # `now` is way past the 5-minute window.
    far_future = 1700000000 + MAX_TIMESTAMP_SKEW_SECONDS + 1
    with pytest.raises(BadSignature, match="timestamp out of range"):
        verify(
            signing_secret=_SECRET, timestamp=ts, signature=sig,
            body=body, now=far_future,
        )


def test_future_timestamp_also_rejected():
    """Clock skew the other way — a client far in the future also fails."""
    body = b'{}'
    ts = "1700001000"
    sig = _signed(body, ts)
    with pytest.raises(BadSignature, match="timestamp out of range"):
        verify(
            signing_secret=_SECRET, timestamp=ts, signature=sig,
            body=body, now=1700000000,  # 1000s before the timestamp
        )


def test_missing_headers_fail():
    with pytest.raises(BadSignature, match="missing signature headers"):
        verify(
            signing_secret=_SECRET, timestamp=None, signature="v0=abc",
            body=b"{}", now=1700000010,
        )
    with pytest.raises(BadSignature, match="missing signature headers"):
        verify(
            signing_secret=_SECRET, timestamp="1700000000", signature=None,
            body=b"{}", now=1700000010,
        )


def test_bad_timestamp_format_fails():
    with pytest.raises(BadSignature, match="bad timestamp"):
        verify(
            signing_secret=_SECRET, timestamp="not-a-number",
            signature="v0=abc", body=b"{}", now=1700000010,
        )


def test_missing_signing_secret_fails():
    """Server misconfiguration — fail fast rather than accepting anything."""
    with pytest.raises(BadSignature, match="no signing secret"):
        verify(
            signing_secret="", timestamp="1700000000",
            signature="v0=abc", body=b"{}", now=1700000010,
        )
