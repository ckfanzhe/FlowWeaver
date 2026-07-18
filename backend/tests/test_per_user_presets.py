"""Per-user LLM preset + MCP server binding.

These tests pin the strict-binding contract:

  * Every preset / MCP server MUST belong to exactly one user. There
    is NO `user_id IS NULL` system tier any more — new users start
    with an empty list and configure their own.
  * `user_id = <X>` rows belong to one user; only that user can
    list/create/update/delete them.
  * `is_default` is per-user — two users can each have their own
    default without fighting over the same row.
  * `user-default` (the no-caller identity) lists nothing and cannot
    create rows — the API returns 400.

Coverage map:
  LLM presets
    * create stamps the caller's id
    * list returns ONLY the caller's rows (never another user's)
    * update/delete are owner-only (404 for non-owners)
    * set_default is per-user (alice's default doesn't clear bob's)
  MCP servers
    * same contract (user-scoping, ownership)
  Runtime resolution
    * `_resolve_default_preset_id(user_id=X)` returns X's default,
      returns None when X has none (no system fallback)
    * `_resolve_preset(id, user_id=X)` is strictly scoped to X
"""
from __future__ import annotations

import uuid

import pytest

ALICE = {"X-User-Id": "alice@example.com"}
BOB = {"X-User-Id": "bob@example.com"}
CAROL = {"X-User-Id": "carol@example.com"}

# ─────────────────────────────────────────────────────────────────
# LLM preset CRUD — per-user scoping
# ─────────────────────────────────────────────────────────────────
def test_create_stamps_owner(client):
    r = client.post(
        "/api/v1/llm-presets",
        headers=ALICE,
        json={"name": "Alice Claude", "provider": "anthropic",
              "modelId": "claude-sonnet-4-5", "apiKey": "sk-alice"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["userId"] == "alice@example.com"

def test_create_without_caller_400(client):
    """No X-User-Id → no-caller identity → 400 (cannot create)."""
    r = client.post(
        "/api/v1/llm-presets",
        json={"name": "Anon", "provider": "openai", "modelId": "gpt-4o"},
    )
    assert r.status_code == 400, r.text

def test_list_returns_only_owner_rows(client):
    """Alice's list shows ONLY her rows — never Bob's, never a system
    row (there is no system tier any more).

    Bob's row is created via the API so we also exercise the create
    path's user_id stamp.
    """
    # Bob's row.
    bob_pid = client.post(
        "/api/v1/llm-presets",
        headers=BOB,
        json={"name": "Bob GPT", "provider": "openai",
              "modelId": "gpt-4o", "apiKey": "sk-bob"},
    ).json()["id"]
    # Alice's row.
    r = client.post(
        "/api/v1/llm-presets",
        headers=ALICE,
        json={"name": "Alice GPT", "provider": "openai",
              "modelId": "gpt-4o", "apiKey": "sk-alice"},
    )
    assert r.status_code == 201

    # Alice's listing.
    rows = client.get("/api/v1/llm-presets", headers=ALICE).json()
    ids = {p["id"] for p in rows}
    assert bob_pid not in ids, "bob's row must NOT be visible to alice"
    assert all(p["userId"] == "alice@example.com" for p in rows)
    # And there are no NULL-user_id rows any more (strict binding).
    assert all(p["userId"] is not None for p in rows)

def test_update_by_other_user_404(client):
    """Bob's row cannot be PATCHed by Alice — strict binding returns
    404 (same response as a non-existent id; no existence leak)."""
    pid = client.post(
        "/api/v1/llm-presets",
        headers=BOB,
        json={"name": "Bob GPT", "provider": "openai", "modelId": "gpt-4o"},
    ).json()["id"]

    r = client.patch(
        f"/api/v1/llm-presets/{pid}",
        headers=ALICE,
        json={"name": "Hijacked"},
    )
    assert r.status_code == 404

def test_update_by_owner_works(client):
    pid = client.post(
        "/api/v1/llm-presets",
        headers=ALICE,
        json={"name": "Alice Claude", "provider": "anthropic",
              "modelId": "claude-sonnet-4-5"},
    ).json()["id"]

    r = client.patch(
        f"/api/v1/llm-presets/{pid}",
        headers=ALICE,
        json={"name": "Renamed"},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed"

def test_delete_by_other_user_404(client):
    pid = client.post(
        "/api/v1/llm-presets",
        headers=BOB,
        json={"name": "Bob", "provider": "openai", "modelId": "gpt-4o"},
    ).json()["id"]
    r = client.delete(f"/api/v1/llm-presets/{pid}", headers=ALICE)
    assert r.status_code == 404
    # The row is still there (delete was refused).
    assert client.get(f"/api/v1/llm-presets/{pid}", headers=BOB).status_code == 200

def test_get_by_other_user_404(client):
    """Non-owners get 404 (not 403) so the API doesn't leak existence."""
    pid = client.post(
        "/api/v1/llm-presets",
        headers=BOB,
        json={"name": "Bob", "provider": "openai", "modelId": "gpt-4o"},
    ).json()["id"]
    r = client.get(f"/api/v1/llm-presets/{pid}", headers=ALICE)
    assert r.status_code == 404

def test_default_is_per_user(client):
    """Alice's default toggle doesn't clear Bob's."""
    a1 = client.post(
        "/api/v1/llm-presets",
        headers=ALICE,
        json={"name": "Alice A", "provider": "openai",
              "modelId": "gpt-4o", "isDefault": True},
    ).json()["id"]
    a2 = client.post(
        "/api/v1/llm-presets",
        headers=ALICE,
        json={"name": "Alice B", "provider": "anthropic",
              "modelId": "claude-sonnet-4-5", "isDefault": False},
    ).json()["id"]
    b1 = client.post(
        "/api/v1/llm-presets",
        headers=BOB,
        json={"name": "Bob A", "provider": "openai",
              "modelId": "gpt-4o", "isDefault": True},
    ).json()["id"]

    # Alice promotes her second row.
    r = client.post(f"/api/v1/llm-presets/{a2}/default", headers=ALICE)
    assert r.status_code == 200
    assert r.json()["isDefault"] is True

    # Alice's listing: a2 default, a1 not.
    alice_rows = {
        p["id"]: p for p in client.get("/api/v1/llm-presets", headers=ALICE).json()
    }
    assert alice_rows[a2]["isDefault"] is True
    assert alice_rows[a1]["isDefault"] is False

    # Bob's listing still has b1 as his default (Alice's toggle didn't reach him).
    bob_rows = {
        p["id"]: p for p in client.get("/api/v1/llm-presets", headers=BOB).json()
    }
    assert bob_rows[b1]["isDefault"] is True

def test_set_default_promoting_other_users_row_404(client):
    pid = client.post(
        "/api/v1/llm-presets",
        headers=BOB,
        json={"name": "Bob", "provider": "openai", "modelId": "gpt-4o"},
    ).json()["id"]
    r = client.post(f"/api/v1/llm-presets/{pid}/default", headers=ALICE)
    assert r.status_code == 404

def test_default_user_sees_nothing(client):
    """The placeholder (`X-User-Id` missing → `user-default`)
    lists nothing — there's no system tier to fall back to. Each
    user must configure their own presets."""
    # Alice's private row — should NOT appear for user-default.
    client.post(
        "/api/v1/llm-presets",
        headers=ALICE,
        json={"name": "Alice", "provider": "openai", "modelId": "gpt-4o"},
    )

    rows = client.get("/api/v1/llm-presets").json()  # no header
    assert rows == [], "user-default (no X-User-Id) must see no presets"

# ─────────────────────────────────────────────────────────────────
# MCP server CRUD — per-user scoping
# ─────────────────────────────────────────────────────────────────
def test_mcp_create_stamps_owner(client):
    r = client.post(
        "/api/v1/mcp-servers",
        headers=ALICE,
        json={"name": "fs", "transport": "stdio", "command": "npx"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["userId"] == "alice@example.com"

def test_mcp_create_without_caller_400(client):
    r = client.post(
        "/api/v1/mcp-servers",
        json={"name": "fs", "transport": "stdio", "command": "npx"},
    )
    assert r.status_code == 400, r.text

def test_mcp_list_returns_only_owner_rows(client):
    bob_id = client.post(
        "/api/v1/mcp-servers",
        headers=BOB,
        json={"name": "Bob FS", "transport": "stdio", "command": "npx"},
    ).json()["id"]
    client.post(
        "/api/v1/mcp-servers",
        headers=ALICE,
        json={"name": "Alice FS", "transport": "stdio", "command": "npx"},
    )

    rows = client.get("/api/v1/mcp-servers", headers=ALICE).json()
    ids = {p["id"] for p in rows}
    assert bob_id not in ids
    assert all(p["userId"] == "alice@example.com" for p in rows)

def test_mcp_update_by_other_user_404(client):
    sid = client.post(
        "/api/v1/mcp-servers",
        headers=BOB,
        json={"name": "Bob", "transport": "stdio", "command": "npx"},
    ).json()["id"]
    r = client.patch(
        f"/api/v1/mcp-servers/{sid}",
        headers=ALICE,
        json={"enabled": False},
    )
    assert r.status_code == 404

def test_mcp_delete_by_other_user_404(client):
    sid = client.post(
        "/api/v1/mcp-servers",
        headers=BOB,
        json={"name": "Bob", "transport": "stdio", "command": "npx"},
    ).json()["id"]
    r = client.delete(f"/api/v1/mcp-servers/{sid}", headers=ALICE)
    assert r.status_code == 404

def test_mcp_get_by_other_user_404(client):
    sid = client.post(
        "/api/v1/mcp-servers",
        headers=BOB,
        json={"name": "Bob", "transport": "stdio", "command": "npx"},
    ).json()["id"]
    r = client.get(f"/api/v1/mcp-servers/{sid}", headers=ALICE)
    assert r.status_code == 404

def test_mcp_default_user_sees_nothing(client):
    """user-default (no X-User-Id) lists nothing — no system tier."""
    client.post(
        "/api/v1/mcp-servers",
        headers=ALICE,
        json={"name": "Alice FS", "transport": "stdio", "command": "npx"},
    )

    rows = client.get("/api/v1/mcp-servers").json()  # no header
    assert rows == [], "user-default must see no MCP servers"

# ─────────────────────────────────────────────────────────────────
# Runtime resolution — per-user scope (no system fallback)
# ─────────────────────────────────────────────────────────────────
def test_resolve_default_preset_id_user_scope(db):
    """`_resolve_default_preset_id(user_id=X)` returns X's default
    strictly; users without a default get None (no system fallback)."""
    from app.db.models import LlmPreset
    from app.core.llm_runner import _resolve_default_preset_id

    alice_default = f"preset-{uuid.uuid4().hex[:8]}"
    bob_default = f"preset-{uuid.uuid4().hex[:8]}"
    db.add(LlmPreset(
        id=alice_default, name="Alice",
        provider="openai", model_id="gpt-4o",
        api_key="sk-a", is_default=True, thinking=False,
        user_id="alice@example.com",
    ))
    db.add(LlmPreset(
        id=bob_default, name="Bob",
        provider="openai", model_id="gpt-4o",
        api_key="sk-b", is_default=True, thinking=False,
        user_id="bob@example.com",
    ))
    db.commit()

    # Alice's default — bob's row is invisible.
    assert _resolve_default_preset_id(db=db, user_id="alice@example.com") == alice_default
    # Bob's default — alice's row is invisible.
    assert _resolve_default_preset_id(db=db, user_id="bob@example.com") == bob_default
    # A user with no default → None (no system fallback in round 2).
    assert _resolve_default_preset_id(db=db, user_id="carol@example.com") is None

def test_resolve_preset_user_scope(db):
    """`_resolve_preset(id, user_id=X)` only returns X's rows. Looking
    up another user's preset id returns None."""
    from app.db.models import LlmPreset
    from app.core.llm_runner import _resolve_preset

    alice_pid = f"preset-{uuid.uuid4().hex[:8]}"
    bob_pid = f"preset-{uuid.uuid4().hex[:8]}"
    db.add(LlmPreset(
        id=alice_pid, name="Alice",
        provider="openai", model_id="gpt-4o",
        api_key="sk-a", is_default=False, thinking=False,
        user_id="alice@example.com",
    ))
    db.add(LlmPreset(
        id=bob_pid, name="Bob",
        provider="openai", model_id="gpt-4o",
        api_key="sk-b", is_default=False, thinking=False,
        user_id="bob@example.com",
    ))
    db.commit()

    # Alice can see her own row. `db=db` makes the lookup use the
    # test's in-memory engine (the production `session_scope()` would
    # open a session against the production engine and see nothing).
    assert _resolve_preset(alice_pid, user_id="alice@example.com", db=db) is not None
    # Alice CANNOT see Bob's row — even with the explicit id.
    assert _resolve_preset(bob_pid, user_id="alice@example.com", db=db) is None
    # Bob can see his own row.
    assert _resolve_preset(bob_pid, user_id="bob@example.com", db=db) is not None
    # Bob CANNOT see Alice's row.
    assert _resolve_preset(alice_pid, user_id="bob@example.com", db=db) is None

def test_ensure_single_default_scoped_per_user(db):
    """`is_default=true` flips every OTHER row owned by the same
    user off, leaving other users' defaults untouched."""
    from app.db.models import LlmPreset
    from app.services import llm_preset_service

    a1 = f"preset-{uuid.uuid4().hex[:8]}"
    a2 = f"preset-{uuid.uuid4().hex[:8]}"
    b1 = f"preset-{uuid.uuid4().hex[:8]}"
    db.add_all([
        LlmPreset(id=a1, name="A1", provider="openai", model_id="gpt-4o",
                  api_key="sk", is_default=True, thinking=False,
                  user_id="alice@example.com"),
        LlmPreset(id=a2, name="A2", provider="openai", model_id="gpt-4o",
                  api_key="sk", is_default=False, thinking=False,
                  user_id="alice@example.com"),
        LlmPreset(id=b1, name="B1", provider="openai", model_id="gpt-4o",
                  api_key="sk", is_default=True, thinking=False,
                  user_id="bob@example.com"),
    ])
    db.commit()

    llm_preset_service._ensure_single_default(db, "alice@example.com", a2)
    db.commit()
    db.expire_all()

    a1_row = db.query(LlmPreset).filter_by(id=a1).one()
    a2_row = db.query(LlmPreset).filter_by(id=a2).one()
    b1_row = db.query(LlmPreset).filter_by(id=b1).one()
    # `_ensure_single_default` only flips OTHER owned rows off; it
    # does NOT promote the chosen row (the caller in
    # `set_default_preset` does that). So after the call: a1 off,
    # a2 unchanged (still False), b1 untouched.
    assert a1_row.is_default is False, "Alice's other row should be off"
    assert a2_row.is_default is False, "ensure_single_default doesn't promote"
    assert b1_row.is_default is True, "Bob's default must not be touched"