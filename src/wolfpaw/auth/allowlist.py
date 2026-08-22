"""Sign-in allowlist. `WOLFPAW_ALLOWED_EMAILS`, comma-separated; empty
means open sign-up."""

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
