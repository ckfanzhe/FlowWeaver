"""User identity service — .

Email IS the user identifier. No tokens, no SMTP, no magic link —
just a normalised `users.id = email` lookup that lets a returning
caller (same email on a new device) re-bind to their workflow list.

Two operations:
  * `identify(email)` — upsert by email. Returns the user row plus
    a `created` flag so the frontend can distinguish a brand-new
    account from a returning one.
  * `get_by_id(user_id)` — fetch the row matching the caller's
    `X-User-Id` header. Returns None for the `user-default`
    placeholder so the API can surface a 404 ("you need to identify
    yourself") instead of echoing a confusing NULL email row.

Why this lives in its own module instead of being a method on
`member_service`: `member_service` is about workflow permissions,
which assume you already have an identity. This module is the
gateway to having one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.auth import DEFAULT_TENANT_ID, DEFAULT_USER_ID
from app.db.models import User

@dataclass(frozen=True)
class IdentifyOutcome:
    """Carrier for `identify()` so callers don't have to import ORM models."""
    user: User
    created: bool

def _normalise_email(raw: str) -> str:
    """Lowercase + strip — case-insensitive lookup, no leading/trailing ws."""
    return (raw or "").strip().lower()

def _looks_like_email(s: str) -> bool:
    """Tiny format check; sufficient for an internal-network flow.

    Pydantic's `EmailStr` would do this AND DNS/MX validation, but
    pulling `email-validator` in as a hard dep just for a single
    field is overkill. Internal-network trusts the operator.
    """
    return "@" in s and "." in s.split("@", 1)[-1]

def identify(
    db: Session,
    email: str,
    *,
    language: str | None = None,
    avatar_id: str | None = None,
    theme: str | None = None,
) -> IdentifyOutcome:
    """Look up `email`, creating or filling a row as needed.

    `User.id` is set to the normalised email so future `X-User-Id`
    header values can resolve without a separate index. This means
    re-typing the email on a new device gives the caller back
    their workflow list — `workflow_members` is keyed on
    `(workflow_id, user_id)`, and `user_id == email` survives
    across browsers.

    Three upsert cases:
      1. No row at all            → INSERT (created=True)
      2. Row exists with `email=None`
        (lazy-created by `current_user` when the frontend first
        sent the `X-User-Id` header) — fill in the email. NOT a
        brand-new account, so `created=False`.
      3. Row exists with the same email → no-op upsert (created=False).

    Case (2) is the recovery path: the frontend found a stale
    `localStorage` id, fired `/users/me`, got `email=null`, and
    now re-identifies to fill the gap. Without the email-fill
    branch the second INSERT would hit the UNIQUE PK on `users.id`.

    Optional kwargs:
      * `language` — ISO 639-1 / BCP-47 short tag (e.g. `"en"`,
        `"zh-CN"`). When supplied, overwrites the stored
        preference. When `None`, the existing value is kept so
        re-identifying from a different device doesn't accidentally
        wipe the preference. (The frontend re-asserts the
        preference on every identify so a returning user on a new
        device picks up the right locale immediately.)
      * `avatar_id` — opaque picker id (e.g. `"fox"`, `"robot"`).
        Same semantics as `language`. The user only ever sends it
        when they actively picked a new avatar in the UserMenu.
      * `theme` — UI theme choice (`"light"`, `"dark"`, `"system"`).
        Same semantics: `None` keeps the existing value, anything
        else overwrites. The UserMenu re-asserts on every change
        so the choice is bound to the user, not to localStorage.
    """
    from fastapi import HTTPException, status

    normalised = _normalise_email(email)
    if not _looks_like_email(normalised):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "valid email required",
        )
    row = (
        db.query(User)
        .filter_by(id=normalised)
        .one_or_none()
    )
    if row is None:
        # Case 1: truly new — `id = email` so the caller's next
        # `X-User-Id` request resolves to the same record.
        row = User(
            id=normalised,
            email=normalised,
            tenant_id=DEFAULT_TENANT_ID,
            language=language,
            avatar_id=avatar_id,
            theme=theme,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return IdentifyOutcome(user=row, created=True)
    # Case 2 & 3: row exists — fill the email if it's still null,
    # then layer the optional preference updates on top.
    # `created=False` either way (the row already existed).
    changed = False
    if row.email is None:
        row.email = normalised
        changed = True
    if language is not None and row.language != language:
        row.language = language
        changed = True
    if avatar_id is not None and row.avatar_id != avatar_id:
        row.avatar_id = avatar_id
        changed = True
    if theme is not None and row.theme != theme:
        row.theme = theme
        changed = True
    if changed:
        db.commit()
        db.refresh(row)
    return IdentifyOutcome(user=row, created=False)

def get_by_id(db: Session, user_id: str) -> User | None:
    """Fetch a user by id (the `X-User-Id` header value).

    Returns None when the row doesn't exist (or when it's the
    `user-default` placeholder — the API layer turns that into a
    404 with a "you need to identify yourself" message).
    """
    if not user_id or user_id == DEFAULT_USER_ID:
        return None
    return db.query(User).filter_by(id=user_id).one_or_none()

__all__ = ["IdentifyOutcome", "identify", "get_by_id"]