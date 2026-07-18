"""Workflow membership API — .

Endpoints:
  GET    /api/v1/workflows/{id}/members              — list members (viewer+)
  POST   /api/v1/workflows/{id}/members              — invite (owner only)
  DELETE /api/v1/workflows/{id}/members/{user_id}    — remove (owner only)

RBAC is delegated to `app.services.member_service.require_role` so
the rules live next to the table logic — endpoints stay thin and
the 403/404 contract is uniform across the API.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import CurrentUser, current_user
from app.db.session import get_db
from app.schemas.member import MemberCreate, MemberRead
from app.services import member_service

router = APIRouter(prefix="/api/v1/workflows", tags=["workflow-members"])

@router.get(
    "/{workflow_id}/members",
    response_model=list[MemberRead],
    response_model_by_alias=False,
)
def list_workflow_members(
    workflow_id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
) -> list[MemberRead]:
    """List every member on `workflow_id`. Requires `viewer` access."""
    member_service.require_role(db, workflow_id, user, "viewer")
    return member_service.list_members(db, workflow_id)

@router.post(
    "/{workflow_id}/members",
    response_model=MemberRead,
    status_code=status.HTTP_201_CREATED,
    response_model_by_alias=False,
)
def invite_member(
    workflow_id: str,
    payload: MemberCreate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
) -> MemberRead:
    """Invite (or re-invite) a member. Requires `owner` access."""
    member_service.require_role(db, workflow_id, user, "owner")
    return member_service.add_member(
        db,
        workflow_id=workflow_id,
        payload=payload,
        invited_by=user.id,
    )

@router.delete(
    "/{workflow_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_workflow_member(
    workflow_id: str,
    user_id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
) -> None:
    """Remove a member. Requires `owner` access; refuses if last owner.

    Self-removal is allowed (an owner can leave their own workflow
    as long as another owner exists). The "last owner" guard lives in
    `member_service.remove_member` so the rule survives any future
    endpoint that wants to remove members.
    """
    member_service.require_role(db, workflow_id, user, "owner")
    try:
        member_service.remove_member(db, workflow_id, user_id)
    except HTTPException:
        raise
    return None

__all__ = ["router"]