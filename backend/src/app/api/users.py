"""User identity API — .

Endpoints:
  POST /api/v1/users/identify  — upsert by email, return the user row
  GET  /api/v1/users/me        — read the caller (from X-User-Id header)

Email is the user identifier. The frontend prompts for an email on
first visit, stores it in localStorage, and forwards it on every
request as `X-User-Id`. On a new device the user types the same
email; this endpoint looks up the existing row so the workflow
list re-hydrates.

Why `identify` (not `login` / `signup`): there's no auth, no
password, no email verification. The endpoint just makes sure the
caller's identity row exists, returning `created=true` on first
sight so the frontend can show a one-time welcome.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import CurrentUser, current_user
from app.db.session import get_db
from app.schemas.user import IdentifyRequest, IdentifyResult, MeResult
from app.services import user_service

router = APIRouter(prefix="/api/v1/users", tags=["users"])

@router.post(
    "/identify",
    response_model=IdentifyResult,
    response_model_by_alias=False,
)
def identify_user(
    payload: IdentifyRequest,
    db: Session = Depends(get_db),
) -> IdentifyResult:
    """Upsert the caller by email. Returns the resolved user + `created` flag.

    Optional `language` / `avatarId` / `theme` fields are layered on
    top of the upsert — the frontend re-asserts them on every
    identify so a returning user on a new device picks up the right
    locale, avatar, and theme before the first render.
    """
    outcome = user_service.identify(
        db,
        payload.email,
        language=payload.language,
        avatar_id=payload.avatarId,
        theme=payload.theme,
    )
    return IdentifyResult(
        userId=outcome.user.id,
        email=outcome.user.email or "",
        tenantId=outcome.user.tenant_id,
        created=outcome.created,
        createdAt=outcome.user.created_at,
        language=outcome.user.language,
        avatarId=outcome.user.avatar_id,
        theme=outcome.user.theme,
    )

@router.get(
    "/me",
    response_model=MeResult,
    response_model_by_alias=False,
)
def get_current_user(
    user: CurrentUser = Depends(current_user),
    db: Session = Depends(get_db),
) -> MeResult:
    """Read the caller. 404 if the X-User-Id header doesn't resolve.

    The frontend uses this on app boot to validate the email still
    in localStorage: a 404 means the caller switched identity or
    the backend was reset, and the frontend should clear
    localStorage and re-prompt.
    """
    row = user_service.get_by_id(db, user.id)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "no user with this id — call /users/identify first",
        )
    return MeResult(
        userId=row.id,
        email=row.email,
        tenantId=row.tenant_id,
        language=row.language,
        avatarId=row.avatar_id,
        theme=row.theme,
    )

__all__ = ["router"]