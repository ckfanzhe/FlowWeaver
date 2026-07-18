"""Tests for LLM preset CRUD + generator integration.

Per-user binding: every mutating test sends
`X-User-Id: alice@example.com` so the API doesn't 400 on the
"no identified caller" guard. Read-only tests that don't need a
caller leave the header off — the no-caller path falls through
to "no caller" and the service restricts the listing to system
rows only.
"""
from __future__ import annotations

import ast

USER = {"X-User-Id": "alice@example.com"}

# ─────────────────────────────────────────────────────────────────
# CRUD
# ─────────────────────────────────────────────────────────────────
def test_create_preset(client):
    r = client.post(
        "/api/v1/llm-presets",
        headers=USER,
        json={
            "name": "Test Claude",
            "provider": "anthropic",
            "model_id": "claude-sonnet-4-5",
            "api_key": "sk-test",
            "is_default": True,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"].startswith("preset-")
    assert body["name"] == "Test Claude"
    # The API response is camelCase to match the
    # frontend's `LlmPreset` TypeScript interface. Snake_case output
    # silently broke the star indicator and the preset edit form.
    assert body["hasApiKey"] is True  # key was set; returned as boolean only
    assert "sk-test" not in r.text      # never echo raw key back
    assert body["isDefault"] is True
    # Per-user ownership: response carries the owner's id.
    assert body["userId"] == "alice@example.com"
    return body["id"]

def test_create_preset_accepts_camel_case_payload(client):
    """Regression: the Settings drawer sends camelCase (`modelId`,
    `apiKey`, `baseUrl`, `isDefault`) — the schema must accept those
    exact keys. Previously this returned 422 with `model_id` missing.
    Snake_case still works (back-compat for cURL / scripts / tests)."""
    r = client.post(
        "/api/v1/llm-presets",
        headers=USER,
        json={
            "name": "Camel Claude",
            "provider": "anthropic",
            "modelId": "claude-sonnet-4-5",
            "apiKey": "sk-camel",
            "baseUrl": "https://api.example.com",
            "isDefault": True,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"].startswith("preset-")
    assert body["name"] == "Camel Claude"
    assert body["hasApiKey"] is True
    assert "sk-camel" not in r.text
    assert body["isDefault"] is True

def test_update_preset_accepts_camel_case_partial(client):
    """PATCH with a camelCase partial body — the schema must accept it."""
    pid = client.post(
        "/api/v1/llm-presets",
        headers=USER,
        json={"name": "A", "provider": "openai", "modelId": "gpt-4o"},
    ).json()["id"]

    r = client.patch(
        f"/api/v1/llm-presets/{pid}",
        headers=USER,
        json={"isDefault": True, "apiKey": "new-key"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["isDefault"] is True
    assert body["hasApiKey"] is True

def test_list_presets(client):
    r = client.get("/api/v1/llm-presets", headers=USER)
    assert r.status_code == 200
    assert isinstance(r.json(), list)

def test_get_preset_by_id(client):
    pid = test_create_preset(client)
    r = client.get(f"/api/v1/llm-presets/{pid}", headers=USER)
    # no GET-by-id endpoint, but the list endpoint should include it
    r2 = client.get("/api/v1/llm-presets", headers=USER)
    ids = [p["id"] for p in r2.json()]
    assert pid in ids

def test_update_preset(client):
    pid = test_create_preset(client)
    r = client.patch(
        f"/api/v1/llm-presets/{pid}",
        headers=USER,
        json={"name": "Renamed"},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed"

def test_set_default_preset_clears_others(client):
    a = test_create_preset(client)  # default
    r = client.post(
        "/api/v1/llm-presets",
        headers=USER,
        json={
            "name": "Second",
            "provider": "openai",
            "model_id": "gpt-4o",
            "is_default": False,
        },
    )
    b = r.json()["id"]
    # promote b
    r = client.post(
        f"/api/v1/llm-presets/{b}/default",
        headers=USER,
    )
    assert r.status_code == 200
    assert r.json()["isDefault"] is True
    # a should now be false
    lst = client.get("/api/v1/llm-presets", headers=USER).json()
    by_id = {p["id"]: p for p in lst}
    assert by_id[a]["isDefault"] is False
    assert by_id[b]["isDefault"] is True

def test_delete_preset(client):
    pid = test_create_preset(client)
    r = client.delete(f"/api/v1/llm-presets/{pid}", headers=USER)
    assert r.status_code == 204
    # second delete → 404
    r = client.delete(f"/api/v1/llm-presets/{pid}", headers=USER)
    assert r.status_code == 404

def test_unknown_preset_404(client):
    r = client.delete("/api/v1/llm-presets/preset-nope", headers=USER)
    assert r.status_code == 404

# ─────────────────────────────────────────────────────────────────
# Generator integration: presetId → env-var lookup
# ─────────────────────────────────────────────────────────────────
def test_generator_uses_os_environ_when_preset_set():
    """When `model.presetId` is set, the generated code should NOT embed
    any api_key string — instead it must read from os.environ."""
    from app.core.compile import to_python_source as render_python

    code = render_python({
        "name": "preset-flow",
        "nodes": [
            {"id": "n2", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "Bot", "config": {
                 "model": {
                     "provider": "anthropic",
                     "modelId": "claude-sonnet-4-5",
                     "presetId": "preset-abc123",  # server will look this up
                 },
                 "instructions": "be helpful",
             }}},
        ],
        "edges": [],
    })
    # should parse cleanly
    ast.parse(code)
    # uses os.environ for key — never hardcodes anything
    assert "os.environ" in code
    assert "ANTHROPIC_API_KEY" in code
    assert "Claude(" in code

def test_generator_legacy_inline_key_still_works():
    """Backwards compat: nodes without presetId still inline apiKey."""
    from app.core.compile import to_python_source as render_python

    code = render_python({
        "name": "legacy",
        "nodes": [
            {"id": "n2", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "Bot", "config": {
                 "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "sk-X"},
                 "instructions": "x",
             }}},
        ],
        "edges": [],
    })
    ast.parse(code)
    assert "sk-X" in code  # legacy path still embeds the key