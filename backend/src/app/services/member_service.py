"""Membership service + RBAC helpers — .

Layered atop the `workflows` / `users` / `workflow_members` tables.
Three jobs:
  1. CRUD on `workflow_members` (list / invite / remove).
  2. RBAC checks: `get_role`, `require_role`, `role_at_least`.
  3. The "first invite becomes owner" / "last owner can't leave"
     invariants the API promises.

Why this lives in its own module instead of `workflow_service.py`:
  `workflow_service` is the read+write path for `Workflow` rows; the
  membership path touches a different table and a different RBAC
  vocabulary. Splitting keeps each module under 300 lines and lets
  tests exercise the RBAC rules without standing up the full CRUD
  surface.
"""
from __future__ import annotations

import uuid
from typing import Literal

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.auth import CurrentUser, ensure_user_row
from app.db.models import User, Workflow, WorkflowMember
from app.schemas.member import ROLE_ORDER, MemberCreate, MemberRead

# ─────────────────────────────────────────────────────────────────
# RBAC primitives
# ─────────────────────────────────────────────────────────────────
def role_at_least(actual: str | None, required: str) -> bool:
    """Return True iff `actual` is at least as privileged as `required`.

    `actual` may be None (no membership row) — that always fails the
    check. Unknown role strings fail closed (return False).
    """
    if actual is None:
        return False
    a = ROLE_ORDER.get(actual)
    r = ROLE_ORDER.get(required)
    if a is None or r is None:
        return False
    return a >= r

def get_role(db: Session, workflow_id: str, user_id: str) -> str | None:
    """Look up a user's role on a workflow. None when not a member."""
    row = (
        db.query(WorkflowMember)
        .filter_by(workflow_id=workflow_id, user_id=user_id)
        .one_or_none()
    )
    return row.role if row else None

def require_role(
    db: Session,
    workflow_id: str,
    user: CurrentUser,
    required: Literal["viewer", "editor", "owner"],
) -> str:
    """Return the caller's role on `workflow_id`, raising 403 if not allowed.

    Side benefit: surfaces a consistent 403/404 split across the API.
      * Workflow missing           → 404
      * Workflow exists, no access → 403

    Callers that want a 404 for hidden rows (e.g. the GET endpoint that
    doesn't want to leak existence to non-members) should compare
    against `get_role(...) == None` themselves.
    """
    row = db.query(Workflow).filter_by(id=workflow_id).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow not found")
    role = get_role(db, workflow_id, user.id)
    if not role_at_least(role, required):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"requires {required!r} role on workflow {workflow_id!r}",
        )
    return role

# ─────────────────────────────────────────────────────────────────
# Membership CRUD
# ─────────────────────────────────────────────────────────────────
def list_members(db: Session, workflow_id: str) -> list[MemberRead]:
    """Return every member of `workflow_id` in stable order.

    Order: `owner` first, then `editor`, then `viewer`, alphabetical
    within each tier. Stable enough for the share dialog; the row
    order doesn't change unless someone re-invites.
    """
    rows = (
        db.query(WorkflowMember)
        .filter_by(workflow_id=workflow_id)
        .all()
    )
    rows.sort(
        key=lambda r: (-ROLE_ORDER.get(r.role, 0), r.user_id),
    )
    return [MemberRead.from_orm_row(r) for r in rows]

def _new_member_id() -> str:
    return f"wfm-{uuid.uuid4().hex[:8]}"

def add_member(
    db: Session,
    workflow_id: str,
    payload: MemberCreate,
    invited_by: str,
) -> MemberRead:
    """Invite (or update) a member's role on `workflow_id`.

    Behaviour:
      * Auto-creates a `users` row for `payload.userId` if missing —
        same lazy-create path the `current_user` dependency uses, so
        the caller doesn't need a pre-existing row.
      * Stamps `invited_by` so the audit trail carries who issued the
        invite (NULL when the creator was added at workflow-create
        time; the workflow-service path uses that path).
      * Re-inviting an existing member UPSERTs the role — the share
        dialog can re-promote a viewer to editor without a separate
        "update member" endpoint.
    """
    ensure_user_row(db, payload.userId)
    row = (
        db.query(WorkflowMember)
        .filter_by(workflow_id=workflow_id, user_id=payload.userId)
        .one_or_none()
    )
    if row is None:
        row = WorkflowMember(
            id=_new_member_id(),
            workflow_id=workflow_id,
            user_id=payload.userId,
            role=payload.role,
            invited_by=invited_by,
        )
        db.add(row)
    else:
        row.role = payload.role
        if invited_by and not row.invited_by:
            row.invited_by = invited_by
    db.commit()
    db.refresh(row)
    return MemberRead.from_orm_row(row)

def remove_member(db: Session, workflow_id: str, user_id: str) -> None:
    """Remove `user_id`'s membership. Raises 409 if it would orphan the row.

    Invariant: every workflow must have at least one owner. The
    "creator leaves their own workflow" case is the most common
    foot-gun — without this check, the share dialog could silently
    lock the workflow out of everyone's reach.

    Self-removal is fine; removing the LAST owner is not. The
    caller can always transfer ownership first (re-invite the
    target as owner, then remove the old owner).
    """
    row = (
        db.query(WorkflowMember)
        .filter_by(workflow_id=workflow_id, user_id=user_id)
        .one_or_none()
    )
    if row is None:
        # Idempotent — DELETE on a non-member returns 204 (matches
        # DELETE semantics elsewhere in the API).
        return
    if row.role == "owner":
        # Count remaining owners; refuse if this is the last one.
        owner_count = (
            db.query(WorkflowMember)
            .filter_by(workflow_id=workflow_id, role="owner")
            .count()
        )
        if owner_count <= 1:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "cannot remove the last owner of a workflow; "
                "promote another member to owner first",
            )
    db.delete(row)
    db.commit()

def bootstrap_owner(db: Session, workflow_id: str, user_id: str) -> None:
    """Insert `(workflow_id, user_id, "owner")` if missing.

    Called by the workflow-create path right after the workflow row
    is inserted. Idempotent — a re-call on an already-owned workflow
    is a no-op (lets the import-json / template-instantiate paths use
    the same helper without coordinating ownership separately).

    `invited_by` stays NULL: the owner was the creator, not an invitee.
    """
    existing = (
        db.query(WorkflowMember)
        .filter_by(workflow_id=workflow_id, user_id=user_id)
        .one_or_none()
    )
    if existing is not None:
        return
    ensure_user_row(db, user_id)
    db.add(WorkflowMember(
        id=_new_member_id(),
        workflow_id=workflow_id,
        user_id=user_id,
        role="owner",
        invited_by=None,
    ))
    db.flush()

__all__ = [
    "role_at_least",
    "get_role",
    "require_role",
    "list_members",
    "add_member",
    "remove_member",
    "bootstrap_owner",
]