"""FastAPI dependencies for resolving the current user from a session cookie.

`get_current_user_id` returns the UUID if a valid session cookie is present,
otherwise None. `require_user_id` raises 401 when missing.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, Request, status

from wolfpaw.auth.tokens import verify_session_token
from wolfpaw.config import get_settings


def get_current_user_id(request: Request) -> UUID | None:
    settings = get_settings()
    raw = request.cookies.get(settings.session_cookie_name)
    if not raw:
        return None
    payload = verify_session_token(raw, secret=settings.secret_key.encode())
    return payload.user_id if payload else None


def require_user_id(request: Request) -> UUID:
    user_id = get_current_user_id(request)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="auth required",
        )
    return user_id
