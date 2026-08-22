"""Sign-in allowlist.

`WOLFPAW_ALLOWED_EMAILS` is a comma-separated list of addresses permitted
to request a magic link. Empty (the default) means "no allowlist" — sign-up
stays open, which is the right default for a single-user self-host where
the deployment isn't reachable from the public internet anyway.

When the list *is* set, a non-listed address gets the same 202 as a listed
one: the caller can't tell an allowed address from a rejected one, so the
allowlist can't be used to enumerate who has access.
"""

from __future__ import annotations

from wolfpaw.config import get_settings


def allowed_emails() -> frozenset[str]:
    """Parsed, normalized allowlist. Empty set means the allowlist is off."""
    raw = get_settings().allowed_emails
    return frozenset(
        part.strip().lower() for part in raw.split(",") if part.strip()
    )


def email_allowed(email: str) -> bool:
    allowed = allowed_emails()
    if not allowed:
        return True
    return email.strip().lower() in allowed
