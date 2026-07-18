"""`X-User-Id` → `CurrentUser` identity resolution.

 — multi-user without login.

The platform does NOT authenticate the caller today. Instead, the
client sends an `X-User-Id` header on every request and the backend
treats it as the caller's identity. The header is intentionally not
cryptographically verified: this module trusts whatever string the
client supplies, with a single fallback (`DEFAULT_USER_ID`) when the
header is absent or empty.

This is the swap point for the future multi-tenant auth layer. The
shape of `CurrentUser` (id + tenant_id) matches what an OAuth/JWT
verifier will produce — once the verifier lands, only the body of
`current_user(...)` changes; every endpoint / service that already
takes a `CurrentUser` keeps working unchanged.

Lazy user creation:
  The first time a new `user_id` shows up, we INSERT a row into
  `users` so downstream membership / RBAC queries have a foreign key
  to point at. Display name + email stay NULL until the real auth
  layer fills them in.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app.db.models import User
from app.db.session import get_db

# Placeholders used by:
#   - the row defaults (`workflows.created_by`, `users.tenant_id`)
#   - the fallback path when the request omits `X-User-Id`
#
# These are NOT secrets. The auth swap will replace them with values
# derived from a verified identity token.
DEFAULT_USER_ID = "user-default"
DEFAULT_TENANT_ID = "tenant-default"

@dataclass(frozen=True)
class CurrentUser:
    """Identity carrier passed through request handlers.

    `id` is the caller's stable user id (matches `users.id` — for human
    callers this is their email). `tenant_id` is reserved for the
    future multi-tenant scope; today every caller shares
    `DEFAULT_TENANT_ID`.

    Frozen so endpoints / services can safely hash / log a request's
    identity without worrying about downstream mutation.
    """
    id: str
    tenant_id: str

def ensure_user_row(
    db: Session, user_id: str, tenant_id: str = DEFAULT_TENANT_ID
) -> None:
    """Create a `users` row for `user_id` if one doesn't exist yet.

    Called by `current_user(...)` on the first sight of a new id so
    downstream FK references (`workflow_members.user_id`,
    `workflow_members.invited_by`) can resolve. Idempotent — a second
    call for the same id is a SELECT no-op.

    The real auth layer will replace this with a pre-seeded row from
    the IdP; the "INSERT only when missing" semantics stay the same.
    """
    if not user_id:
        return
    if db.query(User).filter_by(id=user_id).one_or_none() is not None:
        return
    db.add(User(id=user_id, tenant_id=tenant_id))
    # flush (not commit) — caller controls the transaction.
    db.flush()

def current_user(
    db: Annotated[Session, Depends(get_db)],
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> CurrentUser:
    """FastAPI dependency. Reads `X-User-Id` and returns a `CurrentUser`.

    Usage in a route:
        def my_route(user: CurrentUser = Depends(current_user)): ...

    Behaviour:
      * `X-User-Id` present + non-empty  → use it. Auto-create the
        `users` row if first sight, then commit so the parent
        transaction doesn't carry the INSERT.
      * Header missing / empty          → fall back to `DEFAULT_USER_ID`.
        No DB lookup; the placeholder is enough for the per-request
        `CurrentUser` carrier.

    The real auth swap is a one-function rewrite: replace the header
    read with `verify_jwt(token)`, then call `ensure_user_row` with
    the verified `sub` and tenant claim. Every endpoint signature
    stays the same.
    """
    user_id = (x_user_id or "").strip() or DEFAULT_USER_ID
    if user_id == DEFAULT_USER_ID:
        return CurrentUser(
            id=DEFAULT_USER_ID,
            tenant_id=DEFAULT_TENANT_ID,
        )
    ensure_user_row(db, user_id)
    db.commit()
    row = db.query(User).filter_by(id=user_id).one()
    return CurrentUser(
        id=row.id,
        tenant_id=row.tenant_id,
    )

__all__ = [
    "CurrentUser",
    "current_user",
    "ensure_user_row",
    "DEFAULT_USER_ID",
    "DEFAULT_TENANT_ID",
]