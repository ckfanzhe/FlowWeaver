"""Identity layer — placeholder for the future multi-tenant auth swap.

 introduces multi-user collaboration WITHOUT login.
The frontend (or curl) sends an `X-User-Id` header on every request
and the backend treats it as the caller's identity. This module is
the swap point for the real auth layer — when login lands, only the
header → `CurrentUser` mapping here needs to change. The downstream
code (RBAC checks, `created_by`, membership queries) keeps the same
shape.

Design intent:
  - `current_user(...)` is a FastAPI dependency that returns a
    `CurrentUser`. Endpoints that take it inherit RBAC readiness for
    free.
  - `CurrentUser.id` is always non-None: requests without a header
    collapse to the `"user-default"` placeholder so the unauth case is
    a real user row, not a special branch.
  - `tenant_id` lives on every `CurrentUser` so the future multi-tenant
    layer can scope queries via a single column swap.
"""
from app.auth.identity import (
    DEFAULT_TENANT_ID,
    DEFAULT_USER_ID,
    CurrentUser,
    current_user,
    ensure_user_row,
)

__all__ = [
    "CurrentUser",
    "current_user",
    "ensure_user_row",
    "DEFAULT_USER_ID",
    "DEFAULT_TENANT_ID",
]