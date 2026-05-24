"""User-facing endpoints for viewing + editing the User File.

    GET   /me/profile    → returns the authenticated user's persona row
    PATCH /me/profile    → partial update (any subset of persona_md,
                           preferences, timezone). Bumps `version`.

The web UI for these is step 19; for now the React app is missing but
clients can hit these endpoints directly. The Triage Agent uses the
profile content (loaded by the system-prompt builder on every call) for
tone + routing — preferences are surfaced to the model in plain markdown
rather than driving hardcoded routing rules.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from wolfpaw.auth.deps import require_user_id
from wolfpaw.memory.db import acquire
from wolfpaw.persona import user_profile as up
from wolfpaw.persona.builder import invalidate_profile

router = APIRouter(prefix="/me", tags=["persona"])


class ProfileResponse(BaseModel):
    user_id: str
    version: int
    persona_md: str
    preferences: dict[str, Any]
    timezone: str
    updated_at: str | None

    @classmethod
    def from_dao(cls, p: up.UserProfile) -> "ProfileResponse":
        return cls(
            user_id=str(p.user_id),
            version=p.version,
            persona_md=p.persona_md,
            preferences=dict(p.preferences),
            timezone=p.timezone,
            updated_at=p.updated_at.isoformat() if p.updated_at else None,
        )


class ProfilePatchRequest(BaseModel):
    persona_md: str | None = None
    preferences: dict[str, Any] | None = None
    timezone: str | None = None


@router.get("/profile", response_model=ProfileResponse)
async def get_profile(
    user_id: UUID = Depends(require_user_id),
) -> ProfileResponse:
    async with acquire() as conn:
        profile = await up.get(conn, user_id=user_id)
    if profile is None:
        # The auth flow's get_or_create_user_by_email always seeds a row;
        # if it's missing the user predates that path or the row was
        # manually deleted. Return a defaulted shape rather than 404 so
        # the client can PATCH to populate.
        return ProfileResponse(
            user_id=str(user_id), version=0, persona_md="",
            preferences={}, timezone="UTC", updated_at=None,
        )
    return ProfileResponse.from_dao(profile)


@router.patch("/profile", response_model=ProfileResponse)
async def patch_profile(
    payload: ProfilePatchRequest,
    user_id: UUID = Depends(require_user_id),
) -> ProfileResponse:
    if (
        payload.persona_md is None
        and payload.preferences is None
        and payload.timezone is None
    ):
        raise HTTPException(
            400, "PATCH body must include at least one of: persona_md,"
            " preferences, timezone",
        )
    async with acquire() as conn:
        try:
            updated = await up.update(
                conn,
                user_id=user_id,
                persona_md=payload.persona_md,
                preferences=payload.preferences,
                timezone=payload.timezone,
            )
        except LookupError:
            raise HTTPException(
                404, "no user_profile row — sign in again to seed one",
            )
    # Drop any in-process cache entry so the next agent call sees the
    # updated row immediately (the TTL also self-heals other workers).
    invalidate_profile(user_id)
    return ProfileResponse.from_dao(updated)
