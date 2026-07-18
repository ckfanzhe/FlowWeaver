"""MCP server CRUD endpoints — strictly per-user.

 (round 2): per-user binding is now strict — there is no
system-shared tier any more. Every row carries a non-NULL `user_id`;
`user_id IS NULL` rows are no longer visible via the API and cannot
be created or mutated. This mirrors the `LlmPreset` contract exactly:
the column is required and the service layer treats it as such.

The previous design had `user_id IS NULL` rows as read-only system
servers visible to every user; that is gone. A new user starts with
an empty MCP server list and configures their own.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import CurrentUser, current_user
from app.db.models import McpServer
from app.db.session import get_db
from app.schemas.mcp_server import (
    McpServerCreate,
    McpServerRead,
    McpServerUpdate,
)

router = APIRouter(prefix="/api/v1/mcp-servers", tags=["mcp-servers"])

# Response field names are camelCase (`userId`, `createdAt`) by
# Python field name, but FastAPI defaults to `by_alias=True` and
# would emit snake_case (`user_id`, `created_at`). Set `False` on
# every read so the contract is uniform — same rationale as the
# `llm_presets` router.
_READ_KW = {"response_model_by_alias": False}

def _new_id() -> str:
    return f"mcp-{uuid.uuid4().hex[:8]}"

def _caller_id(caller: "CurrentUser | str | None") -> str | None:
    """Mirror of `llm_preset_service._caller_id` — the placeholder
    (`user-default`) collapses to "no caller" so background callers
    see nothing. Real auth swap will replace this with the verified
    `sub` claim and the function disappears entirely."""
    if caller is None:
        return None
    raw: str | None
    if isinstance(caller, str):
        raw = caller
    else:
        raw = getattr(caller, "id", None)
    if not raw or raw == "user-default":
        return None
    return raw

def _get_owned_row(db: Session, server_id: str, caller_id: str | None) -> McpServer:
    """Fetch the row, enforcing strict per-user visibility.

    Returns 404 for both "doesn't exist" and "belongs to someone else"
    so the API doesn't leak the existence of someone else's server.
    """
    if not caller_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "MCP server not found")
    row = (
        db.query(McpServer)
        .filter(McpServer.id == server_id, McpServer.user_id == caller_id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "MCP server not found")
    return row

@router.get("", response_model=list[McpServerRead], **_READ_KW)
def list_mcp_servers(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    """Return ONLY the caller's MCP servers.

    No caller (`user-default` placeholder or `None`) → empty list.
    Per the contract, the placeholder represents "no identified
    user" and gets nothing — there's no system tier to fall back to.
    Each user configures their own MCP servers.
    """
    caller_id = _caller_id(user)
    if caller_id is None:
        return []
    rows = (
        db.query(McpServer)
        .filter(McpServer.user_id == caller_id)
        .order_by(McpServer.created_at.desc())
        .all()
    )
    return [McpServerRead.from_orm_row(r) for r in rows]

@router.post("", response_model=McpServerRead, status_code=status.HTTP_201_CREATED, **_READ_KW)
def create_mcp_server(
    payload: McpServerCreate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    """Create an MCP server owned by the caller.

    `user_id` is stamped onto the row at insert time. Without an
    identified caller, the API returns 400 — there's no anonymous tier.
    """
    caller_id = _caller_id(user)
    if not caller_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Cannot create an MCP server without an identified caller",
        )
    row = McpServer(
        id=payload.id or _new_id(),
        name=payload.name,
        transport=payload.transport,
        enabled=payload.enabled,
        command=payload.command,
        args=payload.args,
        env=payload.env,
        url=payload.url,
        headers=payload.headers,
        user_id=caller_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return McpServerRead.from_orm_row(row)

@router.get("/{server_id}", response_model=McpServerRead, **_READ_KW)
def get_mcp_server(
    server_id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    """Fetch a single MCP server.

    Strict visibility: the row must be owned by the caller. Non-owners
    get 404 — the same response the API gives for genuinely-missing
    rows, so we don't leak the existence of someone else's server.
    """
    caller_id = _caller_id(user)
    row = _get_owned_row(db, server_id, caller_id)
    return McpServerRead.from_orm_row(row)

@router.patch("/{server_id}", response_model=McpServerRead, **_READ_KW)
def update_mcp_server(
    server_id: str,
    payload: McpServerUpdate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    caller_id = _caller_id(user)
    row = _get_owned_row(db, server_id, caller_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    db.commit()
    db.refresh(row)
    return McpServerRead.from_orm_row(row)

@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mcp_server(
    server_id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    caller_id = _caller_id(user)
    row = _get_owned_row(db, server_id, caller_id)
    db.delete(row)
    db.commit()
    return None