"""Template service — read-only access to the built-in templates
seeded by `_seed_templates()`, plus the one-way `instantiate` path
that clones a template into a fresh user workflow.

Why split this out of `workflow_service`:
   - Templates have stricter invariants than user workflows (read-only
     via the public API; the only write is `_seed_templates()`).
     Keeping them in their own module makes the read-only contract
     visible at the function level.
   - `instantiate` mutates state (creates a fresh user row) — that's
     the template → user transition. It's a service-level operation
     that doesn't fit in `workflow_service.create_*` because the
     caller has provided neither a `WorkflowCreate` nor a name; the
     service has to derive both from the template row.
"""
from __future__ import annotations

import copy

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.auth import CurrentUser
from app.db.models import Workflow
from app.schemas.workflow import TemplateSummary, WorkflowRead
from app.services import member_service
from app.services.workflow_service import new_workflow_id

# ─────────────────────────────────────────────────────────────────
# Read
# ─────────────────────────────────────────────────────────────────
def list_templates(db: Session) -> list[TemplateSummary]:
    """Lightweight gallery view of built-in templates. Returns each
    template's id/name/description/category plus a derived node-type
    summary so the frontend can render cards without downloading the
    full node/edge JSON."""
    rows = (
        db.query(Workflow)
        .filter(Workflow.is_template.is_(True))
        .order_by(Workflow.name.asc())
        .all()
    )
    return [TemplateSummary.from_orm_row(r) for r in rows]

def get_template(db: Session, template_id: str) -> WorkflowRead:
    """Fetch the full template (nodes + edges) so the frontend can
    instantiate it. Refuses to return a non-template row by id."""
    row = db.query(Workflow).filter_by(id=template_id).one_or_none()
    if row is None or not getattr(row, "is_template", False):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Template not found")
    return WorkflowRead.from_orm_row(row)

# ─────────────────────────────────────────────────────────────────
# Instantiate
# ─────────────────────────────────────────────────────────────────
def instantiate(
    db: Session,
    template_id: str,
    user: CurrentUser | None = None,
) -> WorkflowRead:
    """Clone a built-in template into a fresh user workflow.

    The new row gets a fresh `wf-<uuid>` id and a `(copy)`-suffixed name
    so it's obvious in the Load menu that it's a derivative. The
    template itself is untouched — this is the only way a user can edit
    a template's contents.

    : the new row is owned by `user.id` (creating an `"owner"`
    membership row), so the instantiator gets immediate editor access
    without a separate invite.
    """
    template = db.query(Workflow).filter_by(id=template_id).one_or_none()
    if template is None or not getattr(template, "is_template", False):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Template not found")
    created_by = user.id if user is not None else "user-default"
    new_row = Workflow(
        id=new_workflow_id(),
        name=f"{template.name} (copy)",
        description=template.description,
        # Deep copy so the new row owns its own list/dict instances.
        nodes=copy.deepcopy(template.nodes or []),
        edges=copy.deepcopy(template.edges or []),
        is_template=False,
        category=None,
        created_by=created_by,
    )
    db.add(new_row)
    db.flush()
    member_service.bootstrap_owner(db, new_row.id, created_by)
    db.commit()
    db.refresh(new_row)
    return WorkflowRead.from_orm_row(new_row)
