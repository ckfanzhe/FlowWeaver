"""Tests for user-identity endpoints.

Covers:
  POST /api/v1/users/identify  — email-as-identity upsert
  GET  /api/v1/users/me        — fetch caller via `X-User-Id`

These endpoints are how the frontend (no login) makes a stable
caller identity. The contract is "email is the user id", so the
test matrix is:
  * brand new email  → row inserted, `created=true`
  * returning email  → row returned, `created=false`
  * invalid format   → 422 (no row touched)
  * normalisation    → case + whitespace collapsed to lowercase
  * `/users/me` happy path with the resolved `X-User-Id`
  * `/users/me` 404 for unknown caller (frontend uses this to detect
    a stale localStorage after a backend reset)
  * `/users/me` 404 for the anonymous `user-default` placeholder
    (the prompt must have fired before any /me check succeeds)
"""
from __future__ import annotations

# ─────────────────────────────────────────────────────────────────
# POST /api/v1/users/identify
# ─────────────────────────────────────────────────────────────────
def test_identify_creates_user_on_first_sight(client, db):
    from app.db.models import User

    r = client.post(
        "/api/v1/users/identify",
        json={"email": "alice@example.com"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["userId"] == "alice@example.com"
    assert body["email"] == "alice@example.com"
    assert body["tenantId"] == "tenant-default"
    assert body["created"] is True
    assert "createdAt" in body

    row = db.query(User).filter_by(id="alice@example.com").one()
    assert row.email == "alice@example.com"
    assert row.tenant_id == "tenant-default"

def test_identify_upserts_returning_user(client, db):
    """Second call with the same email returns `created=false`."""
    from app.db.models import User

    first = client.post(
        "/api/v1/users/identify",
        json={"email": "bob@example.com"},
    )
    assert first.status_code == 200
    assert first.json()["created"] is True

    second = client.post(
        "/api/v1/users/identify",
        json={"email": "bob@example.com"},
    )
    assert second.status_code == 200
    body = second.json()
    assert body["userId"] == "bob@example.com"
    assert body["created"] is False

    # Row count must be exactly one — no duplicate on repeat.
    rows = db.query(User).filter(User.email == "bob@example.com").all()
    assert len(rows) == 1

def test_identify_normalises_email_case_and_whitespace(client, db):
    """Lookup is case-insensitive; leading/trailing whitespace stripped."""
    from app.db.models import User

    first = client.post(
        "/api/v1/users/identify",
        json={"email": "Carol@Example.com"},
    )
    assert first.status_code == 200
    assert first.json()["userId"] == "carol@example.com"

    # Same email typed in a different case → upsert, not a new row.
    second = client.post(
        "/api/v1/users/identify",
        json={"email": "  carol@EXAMPLE.com  "},
    )
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["userId"] == "carol@example.com"

    rows = db.query(User).filter(User.email == "carol@example.com").all()
    assert len(rows) == 1

def test_identify_rejects_missing_at_sign(client, db):
    """`foo` → no `@` → 422, no row inserted."""
    from app.db.models import User

    r = client.post("/api/v1/users/identify", json={"email": "foo"})
    assert r.status_code == 422
    assert db.query(User).count() == 0

def test_identify_rejects_missing_dot(client, db):
    """`bob@local` → has `@` but no dot in domain → 422."""
    r = client.post(
        "/api/v1/users/identify",
        json={"email": "bob@local"},
    )
    assert r.status_code == 422

def test_identify_rejects_empty_string(client, db):
    r = client.post("/api/v1/users/identify", json={"email": ""})
    assert r.status_code == 422

def test_identify_rejects_missing_field(client, db):
    r = client.post("/api/v1/users/identify", json={})
    assert r.status_code == 422

# ─────────────────────────────────────────────────────────────────
# GET /api/v1/users/me
# ─────────────────────────────────────────────────────────────────
def test_me_returns_identified_user(client, db):
    """After identify, the same email as `X-User-Id` resolves."""
    client.post(
        "/api/v1/users/identify",
        json={"email": "dave@example.com"},
    )
    r = client.get(
        "/api/v1/users/me",
        headers={"X-User-Id": "dave@example.com"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["userId"] == "dave@example.com"
    assert body["email"] == "dave@example.com"
    assert body["tenantId"] == "tenant-default"

def test_me_lazy_creates_row_for_unknown_user_id(client, db):
    """A bare `X-User-Id` (never identified) → 200 with `email=null`.

    The `current_user` dependency lazy-creates a `users` row for any
    non-placeholder header so RBAC FK references resolve. `/users/me`
    surfaces this as `email=null` — the frontend uses that as the
    "you have a stale localStorage id, please re-identify" signal.
    """
    from app.db.models import User

    r = client.get(
        "/api/v1/users/me",
        headers={"X-User-Id": "ghost@example.com"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["userId"] == "ghost@example.com"
    assert body["email"] is None
    assert body["tenantId"] == "tenant-default"

    # And the row really did get created — so the next /users/me
    # call with the same header is still 200, not a fresh insert.
    rows = db.query(User).filter_by(id="ghost@example.com").all()
    assert len(rows) == 1

def test_me_email_field_distinguishes_identified_from_lazy(client):
    """Same header sent before vs after `identify` — only the latter
    carries a non-null `email`. This is the on-the-wire signal the
    frontend uses to decide whether to re-prompt."""
    h = {"X-User-Id": "lacy@example.com"}

    before = client.get("/api/v1/users/me", headers=h)
    assert before.status_code == 200
    assert before.json()["email"] is None

    client.post(
        "/api/v1/users/identify",
        json={"email": "lacy@example.com"},
    )

    after = client.get("/api/v1/users/me", headers=h)
    assert after.status_code == 200
    assert after.json()["email"] == "lacy@example.com"

def test_me_404_when_header_missing(client):
    """No `X-User-Id` header → anonymous fallback has no row → 404.

    The frontend must have prompted + identified before calling
    `/users/me`. The anonymous back-compat path only applies to
    workflow CRUD, not the identity endpoint.
    """
    r = client.get("/api/v1/users/me")
    assert r.status_code == 404

def test_me_404_when_header_is_blank(client):
    r = client.get(
        "/api/v1/users/me",
        headers={"X-User-Id": "   "},
    )
    assert r.status_code == 404

# ─────────────────────────────────────────────────────────────────
# Cross-endpoint: identify then use that identity on workflow CRUD
# ─────────────────────────────────────────────────────────────────
def test_identified_user_owns_new_workflow(client, db):
    """After identify(email), POST /workflows with `X-User-Id: <email>`
    makes that user the workflow owner — same shape as a typed-in
    header from any other client."""
    from app.db.models import WorkflowMember

    client.post(
        "/api/v1/users/identify",
        json={"email": "eve@example.com"},
    )
    r = client.post(
        "/api/v1/workflows",
        json={"name": "After Identify"},
        headers={"X-User-Id": "eve@example.com"},
    )
    assert r.status_code == 201, r.text
    wf_id = r.json()["id"]

    row = (
        db.query(WorkflowMember)
        .filter_by(workflow_id=wf_id)
        .one()
    )
    assert row.user_id == "eve@example.com"
    assert row.role == "owner"

def test_workflow_list_filters_by_identified_user(client, db):
    """Two identified users get disjoint workflow lists — proves the
    email-as-id path plugs into the RBAC scope."""
    # alice's workflow
    client.post(
        "/api/v1/users/identify",
        json={"email": "alice@x.com"},
    )
    a = client.post(
        "/api/v1/workflows",
        json={"name": "alice's flow"},
        headers={"X-User-Id": "alice@x.com"},
    )
    assert a.status_code == 201
    alice_wf = a.json()["id"]

    # frank's workflow
    client.post(
        "/api/v1/users/identify",
        json={"email": "frank@x.com"},
    )
    f = client.post(
        "/api/v1/workflows",
        json={"name": "frank's flow"},
        headers={"X-User-Id": "frank@x.com"},
    )
    assert f.status_code == 201
    frank_wf = f.json()["id"]

    # alice's list contains her flow, not frank's
    r_alice = client.get(
        "/api/v1/workflows",
        headers={"X-User-Id": "alice@x.com"},
    )
    assert r_alice.status_code == 200
    ids_alice = [w["id"] for w in r_alice.json()]
    assert alice_wf in ids_alice
    assert frank_wf not in ids_alice

    # frank's list contains his flow, not alice's
    r_frank = client.get(
        "/api/v1/workflows",
        headers={"X-User-Id": "frank@x.com"},
    )
    assert r_frank.status_code == 200
    ids_frank = [w["id"] for w in r_frank.json()]
    assert frank_wf in ids_frank
    assert alice_wf not in ids_frank

# ─────────────────────────────────────────────────────────────────
# Language preference ( follow-up)
# ─────────────────────────────────────────────────────────────────
def test_identify_stores_language_on_first_sight(client):
    """First identify with a `language` field persists it."""
    r = client.post(
        "/api/v1/users/identify",
        json={"email": "multilingual@example.com", "language": "zh-CN"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["language"] == "zh-CN"

def test_identify_avatar_persists_on_first_sight(client):
    """First identify with an `avatarId` field persists it."""
    r = client.post(
        "/api/v1/users/identify",
        json={"email": "fox@example.com", "avatarId": "fox"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["avatarId"] == "fox"

def test_identify_omitting_language_keeps_existing(client, db):
    """A returning user that doesn't pass `language` keeps their
    stored preference. Lets the frontend re-identify on every page
    load without clobbering what was set elsewhere."""
    from app.db.models import User

    client.post(
        "/api/v1/users/identify",
        json={"email": "keeplang@example.com", "language": "zh-CN"},
    )
    # Re-identify WITHOUT language — must NOT overwrite.
    client.post(
        "/api/v1/users/identify",
        json={"email": "keeplang@example.com"},
    )
    row = db.query(User).filter_by(id="keeplang@example.com").one()
    assert row.language == "zh-CN"

def test_identify_explicit_language_overrides_existing(client, db):
    """When the user actively picks a new language, the frontend
    sends the new value and it overwrites the stored one."""
    from app.db.models import User

    client.post(
        "/api/v1/users/identify",
        json={"email": "switchlang@example.com", "language": "en"},
    )
    client.post(
        "/api/v1/users/identify",
        json={"email": "switchlang@example.com", "language": "zh-CN"},
    )
    row = db.query(User).filter_by(id="switchlang@example.com").one()
    assert row.language == "zh-CN"

def test_me_returns_stored_language(client):
    """`/users/me` echoes the stored language so the frontend can
    apply it before the first render on the next visit."""
    client.post(
        "/api/v1/users/identify",
        json={"email": "reload@example.com", "language": "zh-CN"},
    )
    r = client.get(
        "/api/v1/users/me",
        headers={"X-User-Id": "reload@example.com"},
    )
    assert r.status_code == 200
    assert r.json()["language"] == "zh-CN"

def test_me_returns_stored_avatar(client):
    client.post(
        "/api/v1/users/identify",
        json={"email": "robot@example.com", "avatarId": "robot"},
    )
    r = client.get(
        "/api/v1/users/me",
        headers={"X-User-Id": "robot@example.com"},
    )
    assert r.status_code == 200
    assert r.json()["avatarId"] == "robot"

def test_me_null_language_when_unspecified(client):
    """A user that never set a preference gets `language=null` —
    the frontend falls back to localStorage / browser default."""
    client.post(
        "/api/v1/users/identify",
        json={"email": "nolang@example.com"},
    )
    r = client.get(
        "/api/v1/users/me",
        headers={"X-User-Id": "nolang@example.com"},
    )
    assert r.status_code == 200
    assert r.json()["language"] is None
    assert r.json()["avatarId"] is None

def test_identify_rejects_oversize_language(client):
    """Field validation: language capped at 8 chars (e.g. "zh-CN")."""
    r = client.post(
        "/api/v1/users/identify",
        json={"email": "toolong@example.com", "language": "x" * 9},
    )
    assert r.status_code == 422

def test_identify_rejects_oversize_avatar_id(client):
    r = client.post(
        "/api/v1/users/identify",
        json={"email": "bigavatar@example.com", "avatarId": "x" * 33},
    )
    assert r.status_code == 422

# ─────────────────────────────────────────────────────────────────
# Theme preference 
# ─────────────────────────────────────────────────────────────────
def test_identify_stores_theme_on_first_sight(client):
    """First identify with a `theme` field persists it. The frontend
    binds the UserMenu theme picker to the user row so the choice
    travels across browsers."""
    r = client.post(
        "/api/v1/users/identify",
        json={"email": "dark@example.com", "theme": "dark"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["theme"] == "dark"

def test_identify_omitting_theme_keeps_existing(client):
    """A returning identify without `theme` keeps the stored value."""
    # First identify sets theme=dark.
    client.post(
        "/api/v1/users/identify",
        json={"email": "themer@example.com", "theme": "dark"},
    )
    # Second identify only updates language — theme must survive.
    r = client.post(
        "/api/v1/users/identify",
        json={"email": "themer@example.com", "language": "zh-CN"},
    )
    assert r.status_code == 200
    assert r.json()["theme"] == "dark"
    assert r.json()["language"] == "zh-CN"

def test_identify_explicit_theme_overrides_existing(client):
    """Re-identifying with a different `theme` overwrites."""
    client.post(
        "/api/v1/users/identify",
        json={"email": "switcher@example.com", "theme": "dark"},
    )
    r = client.post(
        "/api/v1/users/identify",
        json={"email": "switcher@example.com", "theme": "light"},
    )
    assert r.json()["theme"] == "light"

def test_me_returns_stored_theme(client):
    """`/users/me` echoes back the stored theme."""
    client.post(
        "/api/v1/users/identify",
        json={"email": "metest@example.com", "theme": "system"},
    )
    r = client.get("/api/v1/users/me", headers={"X-User-Id": "metest@example.com"})
    assert r.status_code == 200
    assert r.json()["theme"] == "system"

def test_identify_rejects_oversize_theme(client):
    r = client.post(
        "/api/v1/users/identify",
        json={"email": "bigtheme@example.com", "theme": "x" * 9},
    )
    assert r.status_code == 422

def test_lazy_then_identify_with_preferences(client, db):
    """The recovery path: lazy-created row (X-User-Id before /identify)
    gets the user's preferences filled in when /identify finally fires."""
    from app.db.models import User

    # First, the frontend sends X-User-Id without identifying → lazy row.
    client.get(
        "/api/v1/users/me",
        headers={"X-User-Id": "recovery@example.com"},
    )
    row = db.query(User).filter_by(id="recovery@example.com").one()
    assert row.email is None
    assert row.language is None

    # Now the user identifies with full preferences.
    r = client.post(
        "/api/v1/users/identify",
        json={
            "email": "recovery@example.com",
            "language": "zh-CN",
            "avatarId": "fox",
        },
    )
    assert r.status_code == 200
    assert r.json()["email"] == "recovery@example.com"
    assert r.json()["language"] == "zh-CN"
    assert r.json()["avatarId"] == "fox"

    # And the row is fully populated — still just one row, no PK clash.
    # Expire the session first so we read from the DB, not the cached
    # copy loaded before the client updated the row.
    db.expire_all()
    rows = db.query(User).filter_by(id="recovery@example.com").all()
    assert len(rows) == 1
    assert rows[0].language == "zh-CN"
    assert rows[0].avatar_id == "fox"