"""Workflow CRUD endpoints — thin shim over `app.services.workflow_service`.

The API layer is intentionally minimal here: parse the request payload,
call the service, return the response model. All business logic
(read-only template guard, import-time connection-rule validation,
file-naming rules, generator dispatch) lives in the service so it can
be tested without standing up a FastAPI app.

Endpoints:
  GET    /api/v1/workflows                       — list (user / templates / all)
  POST   /api/v1/workflows                       — create
  GET    /api/v1/workflows/templates            — list templates
  GET    /api/v1/workflows/templates/{id}       — get template
  POST   /api/v1/workflows/from-template/{id}   — instantiate template
  GET    /api/v1/workflows/{id}                 — get one
  PUT    /api/v1/workflows/{id}                 — replace
  PATCH  /api/v1/workflows/{id}                 — partial update
  DELETE /api/v1/workflows/{id}                 — delete
  GET    /api/v1/workflows/{id}/export          — download .py source
  GET    /api/v1/workflows/{id}/export-json     — download .json envelope
  POST   /api/v1/workflows/import-json          — create from envelope
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.auth import CurrentUser, current_user
from app.db.session import get_db
from app.schemas.workflow import (
    WorkflowCreate,
    WorkflowImport,
    WorkflowRead,
    WorkflowSummary,
    WorkflowUpdate,
)
from app.services import template_service, workflow_service

router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])

# ─────────────────────────────────────────────────────────────────
# List
# ─────────────────────────────────────────────────────────────────
@router.get("", response_model=list[WorkflowSummary])
def list_workflows(
    scope: Literal["user", "templates", "all"] = Query(
        "user",
        description=(
            "`user` (default) returns only user workflows — used by the "
            "Load menu. `templates` returns only built-in templates. "
            "`all` returns everything (for admin/inspection)."
        ),
    ),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    return workflow_service.list_workflows(db, scope=scope, user=user)

# ─────────────────────────────────────────────────────────────────
# Templates
# ─────────────────────────────────────────────────────────────────
@router.get("/templates", response_model=list)
def list_templates(db: Session = Depends(get_db)):
    return template_service.list_templates(db)

@router.get("/templates/{template_id}", response_model=WorkflowRead)
def get_template(template_id: str, db: Session = Depends(get_db)):
    return template_service.get_template(db, template_id)

@router.post(
    "/from-template/{template_id}",
    response_model=WorkflowRead,
    status_code=status.HTTP_201_CREATED,
)
def instantiate_template(
    template_id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    return template_service.instantiate(db, template_id, user=user)

# ─────────────────────────────────────────────────────────────────
# CRUD on a single workflow
# ─────────────────────────────────────────────────────────────────
@router.post("", response_model=WorkflowRead, status_code=status.HTTP_201_CREATED)
def create_workflow(
    payload: WorkflowCreate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    return workflow_service.create_workflow(db, payload, user=user)

@router.get("/{workflow_id}", response_model=WorkflowRead)
def get_workflow(
    workflow_id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    return workflow_service.get_workflow(db, workflow_id, user=user)

@router.put("/{workflow_id}", response_model=WorkflowRead)
def replace_workflow(
    workflow_id: str,
    payload: WorkflowCreate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    return workflow_service.replace_workflow(db, workflow_id, payload, user=user)

@router.patch("/{workflow_id}", response_model=WorkflowRead)
def update_workflow(
    workflow_id: str,
    payload: WorkflowUpdate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    return workflow_service.update_workflow(db, workflow_id, payload, user=user)

@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workflow(
    workflow_id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    workflow_service.delete_workflow(db, workflow_id, user=user)
    return None

# ─────────────────────────────────────────────────────────────────
# Exports
# ─────────────────────────────────────────────────────────────────
@router.get("/{workflow_id}/export")
def export_workflow(
    workflow_id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    """Render the workflow as standalone Python source and return it as a download."""
    try:
        source, filename = workflow_service.export_python(db, workflow_id, user=user)
    except FileNotFoundError as exc:
        # Jinja header template (`backend/templates/workflow.py.jinja`)
        # is missing — almost always a stale Docker image that pre-dates
        # the `COPY backend/templates` line. Without the explicit
        # HTTPException, FastAPI's default 500 handler skips CORS
        # headers and the browser only sees "blocked by CORS", hiding
        # the real cause. Raise a proper 503 so the frontend surfaces
        # the message verbatim.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            (
                "Workflow export is temporarily unavailable — the server "
                "image is missing the Jinja template (workflow.py.jinja). "
                "Rebuild the backend image (`docker compose up --build backend`) "
                "and retry."
            ),
        ) from exc
    return Response(
        content=source,
        media_type="text/x-python",
        headers={
            "Content-Disposition": _ascii_disposition(filename),
        },
    )

@router.get("/{workflow_id}/export-json")
def export_workflow_json(
    workflow_id: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    """Return the workflow as a versioned JSON envelope for sharing."""
    body, filename = workflow_service.export_json(db, workflow_id, user=user)
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": _ascii_disposition(filename),
        },
    )

def _ascii_disposition(filename: str) -> str:
    """Build a `Content-Disposition` header that's safe to latin-1 encode.

    Starlette/Starlette latin-1-encodes every header value, so a
    filename with CJK / emoji / accented letters raises
    `UnicodeEncodeError` and the response crashes with a 500 (which
    the browser then surfaces as a CORS error because the failure
    response carries no `Access-Control-Allow-Origin`). The service
    layer's `safe_name` already filters down to ASCII, but we
    double-check here so a future caller can't break the contract.

    Falls back to `workflow.bin` if the result is somehow empty.
    """
    cleaned = "".join(ch if " " <= ch < "\x7f" and ch not in '"\\' else "_" for ch in (filename or ""))
    cleaned = cleaned.strip("_") or "workflow.bin"
    return f'attachment; filename="{cleaned}"'

@router.post(
    "/import-json",
    response_model=WorkflowRead,
    status_code=status.HTTP_201_CREATED,
)
def import_workflow_json(
    payload: WorkflowImport,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(current_user),
):
    """Create a NEW workflow from a JSON envelope."""
    return workflow_service.import_json(db, payload, user=user)