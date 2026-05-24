"""HMAC verification for Slack request signatures.

Every inbound POST from Slack (Events API + Slash Commands) carries two
headers:
    X-Slack-Request-Timestamp: <unix-seconds>
    X-Slack-Signature:         v0=<hex sha256 hmac>

The signature is over the string `v0:<timestamp>:<raw body>` keyed by
our app's signing secret. The timestamp protects against replay — we
reject anything more than ~5 minutes old.

We MUST hash the raw request body bytes (not the re-serialized JSON):
asyncio frameworks tend to re-encode JSON in ways that differ from
Slack's wire format, which breaks the signature.

The signing secret is per-app, set on the Slack app's "Basic
Information" page; it's distinct from the OAuth client secret. We carry
it in `WOLFPAW_SLACK_SIGNING_SECRET`.
"""

from __future__ import annotations

import hashlib
import hmac
import time

# Slack's documented replay window. Anything older is treated as a stale /
# replayed request and rejected.
MAX_TIMESTAMP_SKEW_SECONDS = 5 * 60


class BadSignature(Exception):
    """Raised when signature verification fails for any reason. The
    caller should return 401/403 — don't leak which check failed."""


def verify(
    *,
    signing_secret: str,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    now: float | None = None,
) -> None:
    """Raise BadSignature if the request isn't authentic + fresh.

    Returns None on success.

    `now` is overridable for tests; defaults to `time.time()`.
    """
    if not signing_secret:
        raise BadSignature("server has no signing secret configured")
    if not timestamp or not signature:
        raise BadSignature("missing signature headers")
    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        raise BadSignature("bad timestamp") from None
    current = now if now is not None else time.time()
    if abs(current - ts_int) > MAX_TIMESTAMP_SKEW_SECONDS:
        raise BadSignature("timestamp out of range")

    basestring = b"v0:" + timestamp.encode("ascii") + b":" + body
    digest = hmac.new(
        signing_secret.encode("utf-8"), basestring, hashlib.sha256,
    ).hexdigest()
    expected = "v0=" + digest
    if not hmac.compare_digest(expected, signature):
        raise BadSignature("signature mismatch")
