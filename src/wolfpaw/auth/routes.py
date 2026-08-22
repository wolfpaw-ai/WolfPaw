"""Auth HTTP routes: magic-link issuance + verification, session inspect, logout.

API surface:
  POST /auth/magic-link  {email}        → 202 (email always-on logging via backend)
  GET  /auth/verify?token=...           → sets session cookie, 200 JSON
  GET  /auth/me                         → 200 user info | 401
  POST /auth/logout                     → clears cookie, 204
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr

from wolfpaw.auth.allowlist import email_allowed
from wolfpaw.auth.deps import require_user_id
from wolfpaw.auth.email_backend import EmailBackend, get_email_backend
from wolfpaw.auth.tokens import (
    hash_magic_link_token,
    mint_session_token,
    new_magic_link_token,
)
from wolfpaw.auth.users import get_or_create_user_by_email
from wolfpaw.config import get_settings
from wolfpaw.memory.db import acquire
from wolfpaw.tracing import get_logger

log = get_logger()
router = APIRouter(prefix="/auth", tags=["auth"])


class MagicLinkRequest(BaseModel):
    email: EmailStr


class VerifyResponse(BaseModel):
    user_id: str
    created: bool


@router.post("/magic-link", status_code=status.HTTP_202_ACCEPTED)
async def request_magic_link(
    payload: MagicLinkRequest,
    email_backend: EmailBackend = Depends(get_email_backend),
) -> dict:
    settings = get_settings()
    if not email_allowed(payload.email):
        # Same 202 as the happy path, and no token row: a caller can't
        # distinguish an allowlisted address from a rejected one.
        log.info("auth.magic_link.rejected", email=payload.email)
        return {"status": "ok"}
    plain, hashed = new_magic_link_token()
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.magic_link_ttl_minutes
    )
    async with acquire() as conn:
        await conn.execute(
            "INSERT INTO magic_link_tokens (email, token_hash, expires_at)"
            " VALUES ($1, $2, $3)",
            payload.email.lower(),
            hashed,
            expires_at,
        )
    # Link to the SPA route, not the API endpoint. `/signin/verify` renders
    # a "Signing you in…" page that POSTs the token to `/auth/verify`,
    # refreshes the auth context, then lands on /chat. Pointing the email
    # straight at the API works — it sets the cookie — but leaves the user
    # staring at a raw JSON body with no way forward.
    verify_url = f"{settings.web_base_url}/signin/verify?token={plain}"
    await email_backend.send(
        to=payload.email,
        subject="Your Wolfpaw sign-in link",
        body=(
            f"Sign in to Wolfpaw: {verify_url}\n\n"
            f"The link expires in {settings.magic_link_ttl_minutes} minutes."
        ),
    )
    log.info("auth.magic_link.issued", email=payload.email)
    return {"status": "ok"}


@router.get("/verify", response_model=VerifyResponse)
async def verify_magic_link(token: str, response: Response) -> VerifyResponse:
    settings = get_settings()
    hashed = hash_magic_link_token(token)
    async with acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, email, expires_at, used_at FROM magic_link_tokens"
                " WHERE token_hash = $1 FOR UPDATE",
                hashed,
            )
            if row is None:
                raise HTTPException(400, "invalid token")
            if row["used_at"] is not None:
                raise HTTPException(400, "token already used")
            if row["expires_at"] <= datetime.now(timezone.utc):
                raise HTTPException(400, "token expired")
            if not email_allowed(row["email"]):
                # Belt-and-braces: catches a token minted before the address
                # was dropped from the allowlist.
                log.info("auth.verify.rejected", email=row["email"])
                raise HTTPException(400, "invalid token")
            await conn.execute(
                "UPDATE magic_link_tokens SET used_at = NOW() WHERE id = $1",
                row["id"],
            )
            user_id, created = await get_or_create_user_by_email(conn, row["email"])
    session = mint_session_token(
        user_id=user_id,
        ttl_seconds=settings.session_ttl_days * 86400,
        secret=settings.secret_key.encode(),
    )
    response.set_cookie(
        key=settings.session_cookie_name,
        value=session,
        max_age=settings.session_ttl_days * 86400,
        httponly=True,
        secure=settings.env != "dev",
        samesite="lax",
    )
    log.info("auth.session.issued", user_id=str(user_id), created=created)
    return VerifyResponse(user_id=str(user_id), created=created)


@router.get("/me")
async def me(user_id=Depends(require_user_id)) -> dict:
    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, display_name, created_at FROM users WHERE id = $1",
            user_id,
        )
    if row is None:
        raise HTTPException(401, "user not found")
    return {
        "id": str(row["id"]),
        "email": row["email"],
        "display_name": row["display_name"],
        "created_at": row["created_at"].isoformat(),
    }


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout() -> Response:
    """Clear the session cookie.

    Attributes must match those used at verify time or browsers keep the
    original cookie.
    """
    settings = get_settings()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        settings.session_cookie_name,
        path="/",
        httponly=True,
        secure=settings.env != "dev",
        samesite="lax",
    )
    return response
