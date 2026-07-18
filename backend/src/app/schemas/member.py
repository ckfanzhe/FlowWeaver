"""Pydantic schemas for workflow membership + role vocabulary.

: workflow collaboration without login.

Role vocabulary:
    owner   — full control. Created automatically when a workflow is
              inserted; the only role that can add / remove other
              members and delete the workflow itself.
    editor  — can read AND write the workflow graph (PUT / PATCH).
    viewer  — read-only access. Can fetch the workflow and export it
              but cannot mutate it.

The hierarchy is strict: `owner > editor > viewer`. RBAC helpers in
`app.services.member_service` compare against this order via
`ROLE_ORDER`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["viewer", "editor", "owner"]
ROLES: tuple[str, ...] = ("viewer", "editor", "owner")

# Hierarchy — index 0 is the LOWEST privilege. `role_at_least(user_role,
# required_role)` checks `ROLE_ORDER[user_role] >= ROLE_ORDER[required_role]`.
ROLE_ORDER: dict[str, int] = {"viewer": 0, "editor": 1, "owner": 2}

class MemberCreate(BaseModel):
    """Invite payload — `POST /api/v1/workflows/{id}/members`.

    The frontend (or curl) supplies `userId` of the person to invite.
    Today this just creates / refreshes a `users` row on the fly; once
    the real auth layer lands, the row will already exist and this
    payload becomes "issue an invite to this verified user".
    """
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    userId: str = Field(min_length=1, max_length=200, alias="userId")
    role: Role = "viewer"

class MemberRead(BaseModel):
    """Read shape — `GET /api/v1/workflows/{id}/members`.

    camelCase on the wire to match the rest of the API (see
    `app.schemas.workflow.WorkflowRead`). `joinedAt` mirrors
    `workflow_members.created_at`.
    """
    model_config = ConfigDict(populate_by_name=True)

    workflowId: str = Field(alias="workflow_id")
    userId: str = Field(alias="user_id")
    role: Role
    invitedBy: str | None = Field(default=None, alias="invited_by")
    tenantId: str = Field(alias="tenant_id")
    joinedAt: datetime = Field(alias="created_at")

    @classmethod
    def from_orm_row(cls, row) -> "MemberRead":
        return cls(
            workflow_id=row.workflow_id,
            user_id=row.user_id,
            role=row.role,
            invited_by=getattr(row, "invited_by", None),
            tenant_id=row.tenant_id,
            created_at=row.created_at,
        )

__all__ = ["Role", "ROLES", "ROLE_ORDER", "MemberCreate", "MemberRead"]