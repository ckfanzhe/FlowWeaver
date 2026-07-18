"""Workflow CRUD service — orchestration logic for the workflow row
lifecycle (create / read / update / delete / list / export).

This service is intentionally thin: it constructs ORM rows, persists
them, and returns read models. Validation of the node graph (per-node
config schema, connection rules) lives one layer up — in the Pydantic
schemas and the explicit `_validate_or_422` call on import — so the
service stays testable without standing up a FastAPI app.

Why split this out of `api/workflows.py`:
   - The API layer should be a thin "request → service → response"
     wrapper. Today's `api/workflows.py` mixes 4 concerns (HTTP
     concerns, ORM concerns, JSON-envelope concerns, file-naming
     concerns) — moving them here makes each file readable in 30
     seconds.
   - Tests can exercise business rules directly (e.g.
     `workflow_service.delete(db, id, require_mutable=True)`) instead
     of round-tripping through HTTP.
"""
from __future__ import annotations

import copy
import re
import uuid
from typing import Literal

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.compile import CompileError, to_python_source
from app.core.connection_rules import ConnectionError, validate_connections
from app.core.compile._helpers.utils import safe_name
from app.core.http_constants import HTTP_422
from app.core.workflow_io import (
    WorkflowSchemaError,
    envelope_to_json,
    parse as parse_envelope,
    serialize as serialize_envelope,
)
from app.db.models import Workflow
from app.schemas.workflow import (
    TemplateSummary,
    WorkflowCreate,
    WorkflowImport,
    WorkflowRead,
    WorkflowSummary,
    WorkflowUpdate,
)

# Identity + membership: service-layer RBAC checks go through
# `member_service` so the rules live next to the table they
# protect. We import lazily inside functions to avoid a hard
# module-load dependency from this file (legacy tests don't need
# the member table to exist, even though the schema is now part
# of `models`).
from app.auth import CurrentUser
from app.services import member_service

# ─────────────────────────────────────────────────────────────────
# Public API — return / contract helpers
# ─────────────────────────────────────────────────────────────────
def new_workflow_id() -> str:
    return f"wf-{uuid.uuid4().hex[:8]}"

def safe_json_filename(name: str) -> str:
    """Turn 'My Flow!' into 'my_flow.json' (same rules as the generator).

    ASCII-only by design — see `app.core.compile._helpers.utils.safe_name`
    for the rationale. `str.isalnum()` accepts CJK characters, which
    silently breaks the HTTP `Content-Disposition` header (latin-1
    can't encode them) and returns a 500 instead of a download.
    """
    out: list[str] = []
    for ch in (name or "").lower():
        if "a" <= ch <= "z" or "0" <= ch <= "9":
            out.append(ch)
        elif ch in (" ", "-", "_"):
            out.append("_")
    s = "".join(out).strip("_") or "workflow"
    s = re.sub(r"\.json$", "", s)
    return f"{s}.json"

# ─────────────────────────────────────────────────────────────────
# Internal guards
# ─────────────────────────────────────────────────────────────────
def _require_mutable(row: Workflow) -> None:
    """Templates are read-only via the public API — only the seed path
    (which uses `db.add()` directly) can write them. Otherwise users
    could destroy a built-in template by saving over it."""
    if getattr(row, "is_template", False):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Built-in templates are read-only. Clone it first to make changes.",
        )

def _validate_or_422(nodes: list[dict], edges: list[dict]) -> None:
    """Run per-node-type connection rules; raise 422 with structured
    `detail={"errors": [...]}` on any violation.

    The shape matches what the frontend's `validateConnections` returns
    so a 422 from this endpoint can be rendered the same way as a
    client-side rejection.
    """
    errors: list[ConnectionError] = validate_connections(nodes, edges)
    if not errors:
        return
    raise HTTPException(
        HTTP_422,
        detail={
            "errors": [e.to_dict() for e in errors],
            "message": "; ".join(e.message for e in errors),
        },
    )

# ─────────────────────────────────────────────────────────────────
# List / get
# ─────────────────────────────────────────────────────────────────
def list_workflows(
    db: Session,
    scope: Literal["user", "templates", "all"] = "user",
    user: CurrentUser | None = None,
) -> list[WorkflowSummary]:
    """Return workflows filtered by `scope`.

    `user` (default) returns only workflows the caller is a member of —
    used by the Load menu. `templates` returns only built-in templates
    (public to all callers). `all` returns everything (for admin /
    inspection — also requires the caller be a member of every row
    they see, since leaking workflow lists to non-members breaks the
    share-dialog privacy model).

    `user` may be None only when callers explicitly bypass the
    identity layer (e.g. a startup hook). The HTTP layer always
    supplies one.
    """
    q = db.query(Workflow)
    if scope == "user":
        q = q.filter(Workflow.is_template.is_(False))
    elif scope == "templates":
        q = q.filter(Workflow.is_template.is_(True))
    if user is not None:
        # RBAC scoping: callers only see workflows they're a
        # member of. `templates` (built-in) rows are visible to
        # everyone — the gallery is public. We union the membership
        # join with the template flag so the gallery always works.
        from sqlalchemy import select
        from app.db.models import WorkflowMember

        member_subq = (
            select(WorkflowMember.workflow_id)
            .where(WorkflowMember.user_id == user.id)
            .scalar_subquery()
        )
        if scope == "templates":
            # already filtered above; no extra constraint.
            pass
        else:
            q = q.filter(
                (Workflow.id.in_(member_subq)) | (Workflow.is_template.is_(True))
            )
    rows = q.order_by(Workflow.updated_at.desc()).all()
    return [WorkflowSummary.from_orm_row(r) for r in rows]

def get_workflow(
    db: Session, workflow_id: str, user: CurrentUser | None = None
) -> WorkflowRead:
    row = db.query(Workflow).filter_by(id=workflow_id).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow not found")
    if user is not None and not bool(getattr(row, "is_template", False)):
        # Non-template rows require viewer access. Templates stay
        # readable to everyone so the gallery / instantiate flow
        # doesn't need a session.
        member_service.require_role(db, workflow_id, user, "viewer")
    return WorkflowRead.from_orm_row(row)

# ─────────────────────────────────────────────────────────────────
# Create / replace / update / delete
# ─────────────────────────────────────────────────────────────────
def create_workflow(
    db: Session,
    payload: WorkflowCreate,
    user: CurrentUser | None = None,
) -> WorkflowRead:
    """Insert a brand-new workflow row.

    Also inserts an `"owner"` row in `workflow_members` for the
    caller so the new workflow is immediately accessible to them.
    `created_by` is stamped with `user.id` so list/permission lookups
    can scope by it without a join.

    Note: connection-rule validation (per-node-type constraints like
    `max_outgoing` or `tool_source` isolation) is intentionally NOT
    run here. Saving a workflow is a draft commit — it must succeed
    even when the canvas is mid-edit (e.g. a router with one branch
    still being wired). The rules are enforced at runtime by
    `validate_workflow` in `app/core/graph.py` and at code-export time
    by `generator.render_python`, where a malformed graph would
    actually break execution. Pydantic still catches structural
    problems here (unknown `NodeType`, missing edge fields).
    """
    nodes = [n.model_dump() for n in payload.nodes]
    edges = [e.model_dump() for e in payload.edges]
    created_by = user.id if user is not None else "user-default"
    row = Workflow(
        id=new_workflow_id(),
        name=payload.name,
        description=payload.description,
        nodes=nodes,
        edges=edges,
        created_by=created_by,
    )
    db.add(row)
    db.flush()  # populate row.id before the member insert
    member_service.bootstrap_owner(db, row.id, created_by)
    db.commit()
    db.refresh(row)
    return WorkflowRead.from_orm_row(row)

def replace_workflow(
    db: Session,
    workflow_id: str,
    payload: WorkflowCreate,
    user: CurrentUser | None = None,
) -> WorkflowRead:
    row = db.query(Workflow).filter_by(id=workflow_id).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow not found")
    _require_mutable(row)
    if user is not None:
        member_service.require_role(db, workflow_id, user, "editor")
    nodes = [n.model_dump() for n in payload.nodes]
    edges = [e.model_dump() for e in payload.edges]
    row.name = payload.name
    row.description = payload.description
    row.nodes = nodes
    row.edges = edges
    db.commit()
    db.refresh(row)
    return WorkflowRead.from_orm_row(row)

def update_workflow(
    db: Session,
    workflow_id: str,
    payload: WorkflowUpdate,
    user: CurrentUser | None = None,
) -> WorkflowRead:
    row = db.query(Workflow).filter_by(id=workflow_id).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow not found")
    _require_mutable(row)
    if user is not None:
        member_service.require_role(db, workflow_id, user, "editor")
    data = payload.model_dump(exclude_unset=True)
    new_nodes = row.nodes if "nodes" not in data else [
        n.model_dump() if hasattr(n, "model_dump") else n for n in data["nodes"]
    ]
    new_edges = row.edges if "edges" not in data else [
        e.model_dump() if hasattr(e, "model_dump") else e for e in data["edges"]
    ]
    row.nodes = new_nodes
    row.edges = new_edges
    for k in ("name", "description"):
        if k in data:
            setattr(row, k, data[k])
    db.commit()
    db.refresh(row)
    return WorkflowRead.from_orm_row(row)

def delete_workflow(
    db: Session,
    workflow_id: str,
    user: CurrentUser | None = None,
) -> None:
    row = db.query(Workflow).filter_by(id=workflow_id).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow not found")
    _require_mutable(row)
    if user is not None:
        member_service.require_role(db, workflow_id, user, "owner")
    db.delete(row)
    db.commit()

# ─────────────────────────────────────────────────────────────────
# Export — Python source (download as `.py`)
# ─────────────────────────────────────────────────────────────────
def export_python(
    db: Session,
    workflow_id: str,
    user: CurrentUser | None = None,
) -> tuple[str, str]:
    """Render the workflow as standalone Python source.

    Returns `(source, filename)`. The API layer wraps these in a
    `Response` with the right `Content-Disposition`.

     — threads `user.id` into `to_python_source` so the
    MCP server lookup inside pass-0 is scoped to the workflow owner
    + shared system rows. Pre-binding callers (`user=None`) keep
    the legacy "any visible row" behaviour.
    """
    row = db.query(Workflow).filter_by(id=workflow_id).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow not found")
    if user is not None and not bool(getattr(row, "is_template", False)):
        member_service.require_role(db, workflow_id, user, "viewer")
    try:
        code = to_python_source(
            {
                "name": row.name,
                "nodes": row.nodes or [],
                "edges": row.edges or [],
            },
            user_id=getattr(user, "id", None) if user is not None else None,
        )
    except CompileError as e:
        raise HTTPException(HTTP_422, str(e))
    filename = f"{safe_name(row.name)}.py"
    return code, filename

# ─────────────────────────────────────────────────────────────────
# JSON import / export — for sharing workflows between users
# ─────────────────────────────────────────────────────────────────
def export_json(
    db: Session,
    workflow_id: str,
    user: CurrentUser | None = None,
) -> tuple[str, str]:
    """Return the workflow as a versioned JSON envelope for sharing."""
    row = db.query(Workflow).filter_by(id=workflow_id).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow not found")
    if user is not None and not bool(getattr(row, "is_template", False)):
        member_service.require_role(db, workflow_id, user, "viewer")
    envelope = serialize_envelope({
        "name": row.name,
        "description": row.description,
        "nodes": row.nodes or [],
        "edges": row.edges or [],
    })
    body = envelope_to_json(envelope)
    return body, safe_json_filename(row.name)

def import_json(
    db: Session,
    payload: WorkflowImport,
    user: CurrentUser | None = None,
) -> WorkflowRead:
    """Create a NEW workflow from a JSON envelope.

    Always creates a fresh row (new id, new createdAt/updatedAt). The
    caller is expected to switch the client to the new id after success.

    We run `validate_connections` on the raw envelope's nodes/edges
    FIRST so connection-rule violations surface with the same
    structured `detail.errors` shape the create/update endpoints
    return. `parse_envelope` will then re-run a full topo/cycle check
    and wrap any remaining schema errors as a plain string.

    The new row is owned by `user.id` so the importer gets
    immediate editor access without a separate invite.
    """
    # Best-effort extraction; the envelope's `workflow` field is the
    # canonical carrier per `workflow_io`. If it's missing or malformed,
    # parse_envelope below will produce a clearer error.
    wf_body = (payload.payload or {}).get("workflow") or {}
    nodes_raw = wf_body.get("nodes") or []
    edges_raw = wf_body.get("edges") or []
    if isinstance(nodes_raw, list) and isinstance(edges_raw, list):
        _validate_or_422(nodes_raw, edges_raw)
    try:
        wf = parse_envelope(payload.payload)
    except WorkflowSchemaError as e:
        raise HTTPException(HTTP_422, str(e))
    created_by = user.id if user is not None else "user-default"
    row = Workflow(
        id=new_workflow_id(),
        name=wf["name"],
        description=wf.get("description"),
        nodes=wf["nodes"],
        edges=wf["edges"],
        created_by=created_by,
    )
    db.add(row)
    db.flush()
    member_service.bootstrap_owner(db, row.id, created_by)
    db.commit()
    db.refresh(row)
    return WorkflowRead.from_orm_row(row)