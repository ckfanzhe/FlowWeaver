"""LLM preset service — strictly per-user CRUD + the single-default guarantee.

Presets are the user-managed pool of named models that the PropertyPanel
Agent form picks from. The runtime (`app.core.llm_runner.build_model`)
reads keys/URLs strictly from the preset row — no environment fallback.

Per-user binding (, refined  round 2):
  Every preset MUST belong to exactly one user. The column is named
  `user_id` and the service layer treats it as required — there is no
  system-shared tier any more. The previous design had
  `user_id IS NULL` rows as read-only system presets visible to every
  user; that is gone. A new user starts with an empty preset list
  and has to add their own.

  `is_default=true` is a SINGLETON scoped to the owner — at most one
  row owned by `user_id` may hold it. When a user toggles one on,
  every other row owned by that user gets flipped off.

  This invariant lives here (rather than at the DB layer) because
  SQLAlchemy's `update()` on a `(user_id, is_default=true)` filter is
  a single transaction step that's clearer as Python than as a CHECK
  constraint.

Identity wiring:
  Every endpoint that mutates or lists a preset goes through this
  service. `list_presets(db, caller)` and the CRUD helpers take a
  `CurrentUser` (or its `id`) — the API layer passes the dep and we
  scope the queries accordingly. The placeholder (`user-default`)
  collapses to "no caller" via `_caller_id` and the API returns an
  empty list (and rejects mutations with 400).
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.db.models import LlmPreset
from app.schemas.llm_preset import (
    LlmPresetCreate,
    LlmPresetRead,
    LlmPresetUpdate,
)

if TYPE_CHECKING:
    from app.auth import CurrentUser

def _new_id() -> str:
    return f"preset-{uuid.uuid4().hex[:8]}"

def _ensure_single_default(db: Session, owner_id: str, preset_id: str) -> None:
    """Unset `is_default` on every other row owned by `owner_id`.

    `owner_id` is the user the chosen preset belongs to — never None
    in the strict-binding model. The function is a no-op when the
    owner is missing, but callers always supply one (the API rejects
    mutations without an identified caller).
    """
    if owner_id is None:
        return
    db.query(LlmPreset).filter(
        LlmPreset.user_id == owner_id,
        LlmPreset.id != preset_id,
    ).update({LlmPreset.is_default: False})

# ─────────────────────────────────────────────────────────────────
# CRUD
# ─────────────────────────────────────────────────────────────────
def list_presets(
    db: Session,
    caller: "CurrentUser | str | None" = None,
) -> list[LlmPresetRead]:
    """Return ONLY the caller's presets, default-first.

    No caller (`user-default` placeholder or `None`) → empty list.
    Per the contract, the placeholder represents "no identified
    user" and gets nothing — there's no system tier to fall back to.
    Each user must configure their own presets.

    Order: `is_default DESC, name ASC`. The default row sits at the
    top of the gallery (the PropertyPanel reads it first when the
    user picks a preset from the picker).
    """
    caller_id = _caller_id(caller)
    if caller_id is None:
        return []
    rows = (
        db.query(LlmPreset)
        .filter(LlmPreset.user_id == caller_id)
        .order_by(LlmPreset.is_default.desc(), LlmPreset.name)
        .all()
    )
    return [LlmPresetRead.from_orm_row(r) for r in rows]

def create_preset(
    db: Session,
    payload: LlmPresetCreate,
    caller: "CurrentUser | str | None" = None,
) -> LlmPresetRead:
    """Create a preset owned by `caller`. The caller's id is stamped
    onto the row at insert time — the row's `user_id` is mandatory.

    `is_default=True` flips every other owned preset off, scoped to
    this caller only. Other users' defaults are untouched.
    """
    caller_id = _caller_id(caller)
    if not caller_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Cannot create a preset without an identified caller",
        )
    row = LlmPreset(
        id=_new_id(),
        name=payload.name,
        provider=payload.provider,
        model_id=payload.model_id,
        api_key=payload.api_key or None,
        base_url=payload.base_url or None,
        is_default=payload.is_default,
        thinking=payload.thinking,
        # Sampling / length knobs. NULL passes through as-is — the
        # column is nullable, and `build_model`'s omit-if-None rule
        # keeps the kwarg out of the agno Model constructor.
        temperature=payload.temperature,
        top_p=payload.top_p,
        max_tokens=payload.max_tokens,
        user_id=caller_id,
    )
    if payload.is_default:
        _ensure_single_default(db, caller_id, row.id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return LlmPresetRead.from_orm_row(row)

def update_preset(
    db: Session,
    preset_id: str,
    payload: LlmPresetUpdate,
    caller: "CurrentUser | str | None" = None,
) -> LlmPresetRead:
    caller_id = _caller_id(caller)
    if not caller_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Cannot update a preset without an identified caller",
        )
    row = (
        db.query(LlmPreset)
        .filter(LlmPreset.id == preset_id, LlmPreset.user_id == caller_id)
        .one_or_none()
    )
    if row is None:
        # Either it doesn't exist or it belongs to someone else — the
        # caller can't tell which, which is the contract (no existence
        # leak across users).
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Preset not found")
    data = payload.model_dump(exclude_unset=True)
    if "is_default" in data and data["is_default"]:
        _ensure_single_default(db, caller_id, row.id)
    for k, v in data.items():
        if k == "api_key":
            # explicit empty string means "clear it"; None means "leave unchanged"
            if v == "":
                row.api_key = None
            elif v is not None:
                row.api_key = v
            continue
        setattr(row, k, v)
    db.commit()
    db.refresh(row)
    return LlmPresetRead.from_orm_row(row)

def get_preset(
    db: Session,
    preset_id: str,
    caller: "CurrentUser | str | None" = None,
) -> LlmPresetRead:
    """Fetch a single preset by id, or 404.

    Strict visibility: the row must be owned by the caller. Non-owners
    get 404 — the same response the API gives for genuinely-missing
    rows, so we don't leak the existence of someone else's preset.
    """
    caller_id = _caller_id(caller)
    if not caller_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Preset not found")
    row = (
        db.query(LlmPreset)
        .filter(LlmPreset.id == preset_id, LlmPreset.user_id == caller_id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Preset not found")
    return LlmPresetRead.from_orm_row(row)

def delete_preset(
    db: Session,
    preset_id: str,
    caller: "CurrentUser | str | None" = None,
) -> None:
    caller_id = _caller_id(caller)
    if not caller_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Cannot delete a preset without an identified caller",
        )
    row = (
        db.query(LlmPreset)
        .filter(LlmPreset.id == preset_id, LlmPreset.user_id == caller_id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Preset not found")
    db.delete(row)
    db.commit()

def set_default_preset(
    db: Session,
    preset_id: str,
    caller: "CurrentUser | str | None" = None,
) -> LlmPresetRead:
    caller_id = _caller_id(caller)
    if not caller_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Cannot set default without an identified caller",
        )
    row = (
        db.query(LlmPreset)
        .filter(LlmPreset.id == preset_id, LlmPreset.user_id == caller_id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Preset not found")
    _ensure_single_default(db, caller_id, row.id)
    row.is_default = True
    db.commit()
    db.refresh(row)
    return LlmPresetRead.from_orm_row(row)

# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────
def _caller_id(caller: "CurrentUser | str | None") -> str | None:
    """Normalise the caller argument to a `users.id` (or None).

    `CurrentUser` and bare string ids both flow through this layer
    because the API endpoints take `CurrentUser = Depends(...)` while
    internal callers (the runtime) often already have the string id
    and want to skip the dep.

    The placeholder (`user-default`) is treated like "no
    caller" — it owns no rows and can't promote a default. The strict
    per-user binding ( round 2) means "no caller" returns
    `None` everywhere: empty listings, 400 on create / update / delete /
    set-default.
    """
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