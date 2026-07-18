"""LLM preset endpoints — thin shim over `app.services.llm_preset_service`.

Endpoints:
  GET    /api/v1/llm-presets                   — list
  GET    /api/v1/llm-presets/{id}              — single preset (re-fetch on edit open)
  POST   /api/v1/llm-presets                   — create
  PATCH  /api/v1/llm-presets/{id}              — update
  DELETE /api/v1/llm-presets/{id}              — delete
  POST   /api/v1/llm-presets/{id}/default      — mark as default

`response_model_by_alias=False` is set on every read because the
schema's Python field names are camelCase (`modelId`, `hasApiKey`,
`baseUrl`, `isDefault`) — exactly what the frontend's TypeScript
`LlmPreset` interface expects. FastAPI defaults to `by_alias=True`,
which would emit the snake_case aliases (`model_id`, `has_api_key`).
That mismatch silently broke the LlmTab star button and the
PresetForm pre-population (P3, ).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.auth import CurrentUser, current_user
from app.db.session import get_db
from app.schemas.llm_preset import (
    LlmPresetCreate,
    LlmPresetRead,
    LlmPresetUpdate,
)
from app.services import llm_preset_service

router = APIRouter(prefix="/api/v1/llm-presets", tags=["llm-presets"])

# Single source of truth — every read endpoint serialises camelCase.
_READ_KW = {"response_model_by_alias": False}

@router.get("", response_model=list[LlmPresetRead], **_READ_KW)
def list_presets(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    # Scoping — caller sees their own presets plus the system rows
    # (`user_id IS NULL`). The placeholder (`user-default`) collapses
    # to "no caller" in the service, so it gets only system rows —
    # matching what curl / unscoped tests see.
    return llm_preset_service.list_presets(db, user)

@router.get("/{preset_id}", response_model=LlmPresetRead, **_READ_KW)
def get_preset(
    preset_id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    """Fetch a single preset.

    Used by the Settings → PresetForm editor when the user clicks
    "Edit" — guarantees the form opens against the freshest row
    rather than whatever's cached in the frontend store.
    """
    return llm_preset_service.get_preset(db, preset_id, user)

@router.post("", response_model=LlmPresetRead, status_code=status.HTTP_201_CREATED, **_READ_KW)
def create_preset(
    payload: LlmPresetCreate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    return llm_preset_service.create_preset(db, payload, user)

@router.patch("/{preset_id}", response_model=LlmPresetRead, **_READ_KW)
def update_preset(
    preset_id: str,
    payload: LlmPresetUpdate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    return llm_preset_service.update_preset(db, preset_id, payload, user)

@router.delete("/{preset_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_preset(
    preset_id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    llm_preset_service.delete_preset(db, preset_id, user)
    return None

@router.post("/{preset_id}/default", response_model=LlmPresetRead, **_READ_KW)
def set_default_preset(
    preset_id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    return llm_preset_service.set_default_preset(db, preset_id, user)