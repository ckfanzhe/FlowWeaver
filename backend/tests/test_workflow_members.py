"""Tests for workflow membership + RBAC.

Each test stands up two simulated users via the `X-User-Id` header
(`alice` and `bob`) so the share / permission flows are exercised
without any auth ceremony. The default user (no header) gets its own
back-compat cases.

Coverage map:
    Identity layer
      * default user fallback
      * explicit user auto-creates a `users` row
    Workflow bootstrap
      * creator gets an `"owner"` member row + `created_by`
      * instantiated template + imported JSON also bootstrap owner
    Membership CRUD
      * list returns owners first, then editors, then viewers
      * invite creates the row + lazy-creates the user
      * re-invite UPSERTs the role
      * remove is idempotent on non-members
      * last-owner guard refuses the removal
    RBAC on workflow CRUD
      * viewer cannot PUT / PATCH / DELETE
      * editor can PUT / PATCH but not DELETE
      * owner can DELETE
      * non-member gets 404 on GET (does not leak existence) — wait,
        our GET returns 404 to non-members; verify the contract.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy.orm import sessionmaker

from app.db import session as session_module
from app.main import _seed_templates

# ─────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────
@pytest.fixture()
def seeded(engine):
    """Seed the built-in templates into the in-memory test DB.

    Mirrors `tests/test_templates_api.py::seeded` — copied here so
    the RBAC tests don't have to depend on a sibling test file's
    globals. (A future refactor could move the fixture into conftest.)
    """
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    @contextmanager
    def _patched_scope():
        s = SessionLocal()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    original = session_module.session_scope
    session_module.session_scope = _patched_scope
    try:
        _seed_templates()
    finally:
        session_module.session_scope = original
    yield

# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────
def _create(client, name="Flow", user="alice", nodes=None, edges=None):
    """Create a workflow as `user`; return its id."""
    r = client.post(
        "/api/v1/workflows",
        json={
            "name": name,
            "nodes": nodes or [],
            "edges": edges or [],
        },
        headers={"X-User-Id": user},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]

def _invite(client, wf_id, *, inviter, invitee, role="viewer"):
    body = {"userId": invitee, "role": role}
    return client.post(
        f"/api/v1/workflows/{wf_id}/members",
        json=body,
        headers={"X-User-Id": inviter},
    )

# ─────────────────────────────────────────────────────────────────
# Identity layer
# ─────────────────────────────────────────────────────────────────
def test_current_user_default_when_header_missing(client, db):
    """No `X-User-Id` header → caller collapses to `user-default`.

    The HTTP response shape doesn't expose `created_by` today, so we
    probe the membership row that the create path inserts.
    """
    from app.db.models import WorkflowMember

    r = client.post("/api/v1/workflows", json={"name": "Anon"})
    assert r.status_code == 201, r.text
    wf_id = r.json()["id"]
    row = (
        db.query(WorkflowMember)
        .filter_by(workflow_id=wf_id)
        .one()
    )
    assert row.user_id == "user-default"
    assert row.role == "owner"

def test_explicit_header_lazy_creates_user_row(client, db):
    """A new `X-User-Id` value lazily creates a `users` row."""
    from app.db.models import User

    r = client.post(
        "/api/v1/workflows",
        json={"name": "Hi"},
        headers={"X-User-Id": "carol-1"},
    )
    assert r.status_code == 201, r.text
    row = db.query(User).filter_by(id="carol-1").one_or_none()
    assert row is not None
    assert row.tenant_id == "tenant-default"

def test_explicit_header_does_not_overwrite_existing_user(client, db):
    """Re-using an existing user_id is idempotent — no second row."""
    from app.db.models import User

    # First sight creates the row.
    client.post(
        "/api/v1/workflows",
        json={"name": "Hi1"},
        headers={"X-User-Id": "dave-1"},
    )
    first_row = db.query(User).filter_by(id="dave-1").one()
    first_updated_at = first_row.updated_at

    # Second sight must NOT create a duplicate row.
    r = client.post(
        "/api/v1/workflows",
        json={"name": "Hi2"},
        headers={"X-User-Id": "dave-1"},
    )
    assert r.status_code == 201
    rows = db.query(User).filter_by(id="dave-1").all()
    assert len(rows) == 1
    assert rows[0].updated_at == first_updated_at

# ─────────────────────────────────────────────────────────────────
# Workflow bootstrap
# ─────────────────────────────────────────────────────────────────
def test_creator_becomes_owner(client, db):
    """POST /workflows inserts an `'owner'` member row for the caller."""
    from app.db.models import WorkflowMember

    wf_id = _create(client, "Bootstrap", user="alice")
    members = (
        db.query(WorkflowMember)
        .filter_by(workflow_id=wf_id)
        .all()
    )
    assert len(members) == 1
    assert members[0].user_id == "alice"
    assert members[0].role == "owner"

def test_template_instantiate_grants_owner(client, db, seeded):
    """`POST /workflows/from-template/{id}` bootstraps owner = caller."""
    from app.db.models import WorkflowMember

    r = client.post(
        "/api/v1/workflows/from-template/tpl-hello-world",
        headers={"X-User-Id": "alice"},
    )
    assert r.status_code == 201, r.text
    wf_id = r.json()["id"]
    members = (
        db.query(WorkflowMember)
        .filter_by(workflow_id=wf_id)
        .all()
    )
    assert len(members) == 1
    assert members[0].user_id == "alice"
    assert members[0].role == "owner"

def test_template_instantiate_owner_can_run(client, seeded):
    """Regression: logged-in user creates a workflow from
    a template and runs it under the SAME `X-User-Id` → must succeed.

    The user-visible bug was a frontend regression where
    `runWorkflowStream` used raw `fetch()` instead of `api.fetchRaw()`,
    so the `X-User-Id` header never reached the runtime. The backend
    then collapsed the caller to `user-default`, which has no member
    row on the freshly-instantiated workflow → 403. (See the comment
    on `runWorkflowStream` in `frontend/src/api/workflows.ts`.) The
    backend contract — instantiate-as-X must grant X viewer access —
    is what we pin here so a future backend regression in
    `runtime_service.run_workflow`'s membership gate would also be
    caught.
    """
    r = client.post(
        "/api/v1/workflows/from-template/tpl-hello-world",
        headers={"X-User-Id": "alice"},
    )
    assert r.status_code == 201, r.text
    wf_id = r.json()["id"]

    # Same caller, same header. Must pass the `viewer` gate.
    r = client.post(
        "/api/v1/runtime/run",
        json={"workflow_id": wf_id, "input": "hi"},
        headers={"X-User-Id": "alice"},
    )
    assert r.status_code == 200, (
        f"instantiate-then-run with matching X-User-Id should pass "
        f"the viewer gate, got {r.status_code} {r.text}"
    )
    # Sanity: an SSE session id was issued.
    assert r.headers.get("x-session-id"), (
        f"expected X-Session-Id on run response; headers={dict(r.headers)}"
    )

def test_template_instantiate_other_user_cannot_run(client, seeded):
    """Regression : the owner from instantiation is the
    ONLY account that can run it by default. Bob's `X-User-Id` does
    NOT inherit access — the template copy is private to whoever
    instantiated it (this is the design)."""
    r = client.post(
        "/api/v1/workflows/from-template/tpl-hello-world",
        headers={"X-User-Id": "alice"},
    )
    assert r.status_code == 201, r.text
    wf_id = r.json()["id"]

    r = client.post(
        "/api/v1/runtime/run",
        json={"workflow_id": wf_id, "input": "hi"},
        headers={"X-User-Id": "bob"},
    )
    # `require_role(..., "viewer")` raises 403; surface shape stays
    # consistent with `from-template` cloning being private.
    assert r.status_code == 403, (
        f"non-owner instantiator must not be able to run; got "
        f"{r.status_code} {r.text}"
    )

def test_import_json_grants_owner(client, db):
    """`POST /workflows/import-json` bootstraps owner = caller."""
    from app.db.models import WorkflowMember

    payload = {
        "payload": {
            "schemaVersion": "1.0",
            "kind": "agnobuilder.workflow",
            "exportedAt": "-15T00:00:00Z",
            "workflow": {
                "name": "Imported",
                "description": None,
                "nodes": [
                    {
                        "id": "n1", "type": "agent",
                        "position": {"x": 0, "y": 0},
                        "data": {"label": "Bot", "config": {"instructions": "x"}},
                    },
                ],
                "edges": [],
            },
        },
    }
    r = client.post(
        "/api/v1/workflows/import-json",
        json=payload,
        headers={"X-User-Id": "alice"},
    )
    assert r.status_code == 201, r.text
    wf_id = r.json()["id"]
    members = (
        db.query(WorkflowMember)
        .filter_by(workflow_id=wf_id)
        .all()
    )
    assert len(members) == 1
    assert members[0].role == "owner"

# ─────────────────────────────────────────────────────────────────
# Membership CRUD
# ─────────────────────────────────────────────────────────────────
def test_invite_owner_only(client):
    """Only owners can invite."""
    wf_id = _create(client, user="alice")
    # Promote bob to editor via alice
    assert _invite(client, wf_id, inviter="alice", invitee="bob", role="editor").status_code == 201
    # Editor tries to invite → 403
    r = _invite(client, wf_id, inviter="bob", invitee="carol", role="viewer")
    assert r.status_code == 403
    # Non-member tries to invite → 403
    r = _invite(client, wf_id, inviter="eve", invitee="frank", role="viewer")
    assert r.status_code == 403

def test_invite_creates_lazy_user(client, db):
    """Inviting a brand-new user_id creates a `users` row automatically."""
    from app.db.models import User

    wf_id = _create(client, user="alice")
    r = _invite(client, wf_id, inviter="alice", invitee="ghost", role="viewer")
    assert r.status_code == 201, r.text
    user_row = db.query(User).filter_by(id="ghost").one()
    assert user_row.tenant_id == "tenant-default"

def test_reinvite_overrides_role(client, db):
    """Inviting an existing member UPSERTs the role."""
    from app.db.models import WorkflowMember

    wf_id = _create(client, user="alice")
    _invite(client, wf_id, inviter="alice", invitee="bob", role="viewer")
    _invite(client, wf_id, inviter="alice", invitee="bob", role="editor")
    row = (
        db.query(WorkflowMember)
        .filter_by(workflow_id=wf_id, user_id="bob")
        .one()
    )
    assert row.role == "editor"

def test_list_members_orders_owner_first(client):
    wf_id = _create(client, user="alice")
    _invite(client, wf_id, inviter="alice", invitee="bob", role="editor")
    _invite(client, wf_id, inviter="alice", invitee="carol", role="viewer")
    r = client.get(
        f"/api/v1/workflows/{wf_id}/members",
        headers={"X-User-Id": "alice"},
    )
    assert r.status_code == 200, r.text
    rows = r.json()
    # Order: owner (alice) → editor (bob) → viewer (carol).
    assert [m["userId"] for m in rows] == ["alice", "bob", "carol"]
    assert [m["role"] for m in rows] == ["owner", "editor", "viewer"]

def test_list_members_visible_to_viewer_and_above(client):
    wf_id = _create(client, user="alice")
    _invite(client, wf_id, inviter="alice", invitee="bob", role="viewer")
    r = client.get(
        f"/api/v1/workflows/{wf_id}/members",
        headers={"X-User-Id": "bob"},
    )
    assert r.status_code == 200

def test_list_members_404_for_non_workflow(client):
    r = client.get(
        "/api/v1/workflows/wf-nope/members",
        headers={"X-User-Id": "alice"},
    )
    assert r.status_code == 404

def test_list_members_403_for_non_member(client):
    wf_id = _create(client, user="alice")
    r = client.get(
        f"/api/v1/workflows/{wf_id}/members",
        headers={"X-User-Id": "eve"},
    )
    assert r.status_code == 403

def test_remove_owner_only(client):
    wf_id = _create(client, user="alice")
    _invite(client, wf_id, inviter="alice", invitee="bob", role="editor")
    # Editor cannot remove
    r = client.delete(
        f"/api/v1/workflows/{wf_id}/members/alice",
        headers={"X-User-Id": "bob"},
    )
    assert r.status_code == 403
    # Owner can remove the editor
    r = client.delete(
        f"/api/v1/workflows/{wf_id}/members/bob",
        headers={"X-User-Id": "alice"},
    )
    assert r.status_code == 204
    # Re-removing is idempotent (204)
    r = client.delete(
        f"/api/v1/workflows/{wf_id}/members/bob",
        headers={"X-User-Id": "alice"},
    )
    assert r.status_code == 204

def test_remove_last_owner_refused(client):
    """The sole owner cannot be removed."""
    wf_id = _create(client, user="alice")
    # Add a second owner first so we can test removing the last one
    _invite(client, wf_id, inviter="alice", invitee="bob", role="owner")
    # Removing alice (still 1 owner left = bob) is fine.
    r = client.delete(
        f"/api/v1/workflows/{wf_id}/members/alice",
        headers={"X-User-Id": "bob"},
    )
    assert r.status_code == 204
    # Now bob is the only owner. Removing bob must fail.
    r = client.delete(
        f"/api/v1/workflows/{wf_id}/members/bob",
        headers={"X-User-Id": "bob"},
    )
    assert r.status_code == 409

# ─────────────────────────────────────────────────────────────────
# RBAC on workflow CRUD
# ─────────────────────────────────────────────────────────────────
def test_viewer_can_get_but_cannot_mutate(client):
    wf_id = _create(client, user="alice")
    _invite(client, wf_id, inviter="alice", invitee="bob", role="viewer")

    # GET works
    assert client.get(
        f"/api/v1/workflows/{wf_id}",
        headers={"X-User-Id": "bob"},
    ).status_code == 200

    # PATCH is forbidden
    assert client.patch(
        f"/api/v1/workflows/{wf_id}",
        json={"name": "nope"},
        headers={"X-User-Id": "bob"},
    ).status_code == 403

    # PUT is forbidden
    assert client.put(
        f"/api/v1/workflows/{wf_id}",
        json={"name": "nope", "nodes": [], "edges": []},
        headers={"X-User-Id": "bob"},
    ).status_code == 403

    # DELETE is forbidden
    assert client.delete(
        f"/api/v1/workflows/{wf_id}",
        headers={"X-User-Id": "bob"},
    ).status_code == 403

def test_editor_can_patch_but_cannot_delete(client):
    wf_id = _create(client, user="alice")
    _invite(client, wf_id, inviter="alice", invitee="bob", role="editor")

    # PATCH works
    r = client.patch(
        f"/api/v1/workflows/{wf_id}",
        json={"description": "edit"},
        headers={"X-User-Id": "bob"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "edit"

    # DELETE is forbidden
    assert client.delete(
        f"/api/v1/workflows/{wf_id}",
        headers={"X-User-Id": "bob"},
    ).status_code == 403

def test_non_member_cannot_get(client):
    wf_id = _create(client, user="alice")
    r = client.get(
        f"/api/v1/workflows/{wf_id}",
        headers={"X-User-Id": "eve"},
    )
    assert r.status_code == 403

def test_non_member_cannot_see_in_list(client):
    """Eve has no membership → her `list_workflows(scope=user)` excludes alice's row."""
    wf_id = _create(client, user="alice")
    r = client.get(
        "/api/v1/workflows",
        headers={"X-User-Id": "eve"},
    )
    assert r.status_code == 200
    ids = [w["id"] for w in r.json()]
    assert wf_id not in ids

def test_member_sees_own_workflow_in_list(client):
    wf_id = _create(client, user="alice")
    r = client.get(
        "/api/v1/workflows",
        headers={"X-User-Id": "alice"},
    )
    assert r.status_code == 200
    ids = [w["id"] for w in r.json()]
    assert wf_id in ids

def test_templates_visible_to_everyone(client, seeded):
    """Built-in templates remain public — `list(scope=user)` includes them
    for non-members too (the gallery never had a gate)."""
    r = client.get(
        "/api/v1/workflows?scope=templates",
        headers={"X-User-Id": "eve"},
    )
    assert r.status_code == 200
    ids = [w["id"] for w in r.json()]
    # The seeded gallery has at least one template.
    assert any(i.startswith("tpl-") for i in ids)

# ─────────────────────────────────────────────────────────────────
# Export RBAC
# ─────────────────────────────────────────────────────────────────
def test_export_blocked_for_non_member(client):
    wf_id = _create(client, user="alice")
    r = client.get(
        f"/api/v1/workflows/{wf_id}/export",
        headers={"X-User-Id": "eve"},
    )
    assert r.status_code == 403

def test_export_allowed_for_viewer(client):
    wf_id = _create(
        client, user="alice",
        nodes=[
            {
                "id": "n1", "type": "agent",
                "position": {"x": 0, "y": 0},
                "data": {"label": "Bot", "config": {
                    "model": {"provider": "openai", "modelId": "gpt-4o"},
                    "instructions": "x",
                }},
            },
        ],
        edges=[],
    )
    _invite(client, wf_id, inviter="alice", invitee="bob", role="viewer")
    r = client.get(
        f"/api/v1/workflows/{wf_id}/export",
        headers={"X-User-Id": "bob"},
    )
    assert r.status_code == 200
    assert "text/x-python" in r.headers.get("content-type", "")

def test_export_after_from_template_with_user_id(client, db, seeded):
    """Regression: a workflow created via
    `/workflows/from-template/{id}` with `X-User-Id: <email>` should
    immediately be exportable by the same caller.

    Root cause of the bug: the frontend's `exportPython` /
    `exportJson` originally used raw `fetch()` (not the `api`
    wrapper), so the `X-User-Id` header was never sent. The backend
    fell back to `user-default`, which has no member row for the
    workflow → 403. This test pins the server-side contract:
    whoever the caller was when they instantiated the template is
    the owner who can export it.
    """
    # Create the workflow via the template path with a real X-User-Id.
    r = client.post(
        "/api/v1/workflows/from-template/tpl-hello-world",
        headers={"X-User-Id": "creator@example.com"},
    )
    assert r.status_code == 201, r.text
    wf_id = r.json()["id"]

    # The owner member row should exist for the caller.
    from app.db.models import WorkflowMember
    member = (
        db.query(WorkflowMember)
        .filter_by(workflow_id=wf_id)
        .one()
    )
    assert member.user_id == "creator@example.com"
    assert member.role == "owner"

    # And the export endpoints must succeed for that same caller —
    # both .py and .json paths, since they share the RBAC contract.
    py = client.get(
        f"/api/v1/workflows/{wf_id}/export",
        headers={"X-User-Id": "creator@example.com"},
    )
    assert py.status_code == 200, py.text
    assert "text/x-python" in py.headers.get("content-type", "")

    js = client.get(
        f"/api/v1/workflows/{wf_id}/export-json",
        headers={"X-User-Id": "creator@example.com"},
    )
    assert js.status_code == 200, js.text
    assert "application/json" in js.headers.get("content-type", "")

def test_export_after_from_template_blocks_anonymous(client, seeded):
    """Companion to the regression above: when the caller drops the
    `X-User-Id` header (e.g. a frontend bug that uses raw `fetch`
    instead of the `api` wrapper), the export 403s. This proves the
    guard works as a backstop — the fix has to be on the client
    side, not by loosening the server check."""
    r = client.post(
        "/api/v1/workflows/from-template/tpl-hello-world",
        headers={"X-User-Id": "creator@example.com"},
    )
    assert r.status_code == 201
    wf_id = r.json()["id"]

    # No X-User-Id header → backend resolves caller as user-default →
    # no member row → 403. The frontend MUST send the header for
    # workflows created by an identified user.
    blocked = client.get(f"/api/v1/workflows/{wf_id}/export")
    assert blocked.status_code == 403

# ─────────────────────────────────────────────────────────────────
# RBAC primitives (unit tests)
# ─────────────────────────────────────────────────────────────────
def test_role_at_least_helpers():
    from app.services.member_service import role_at_least
    assert role_at_least("owner", "viewer") is True
    assert role_at_least("owner", "editor") is True
    assert role_at_least("owner", "owner") is True
    assert role_at_least("editor", "owner") is False
    assert role_at_least("viewer", "editor") is False
    assert role_at_least(None, "viewer") is False
    assert role_at_least("owner", "ghost") is False
    assert role_at_least("ghost", "owner") is False