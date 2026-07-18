"""MCP server CRUD endpoint tests.

 — per-user binding: every test that creates / mutates an
MCP server sends `X-User-Id` so the row is owned by a real user and
the API doesn't 400 on the "no identified caller" guard. The
existing pre-binding tests were updated to use a stable `alice`
header — the read-only `test_list_empty` and `test_get_not_found_404`
don't need a header because they don't trigger the guard.
"""
from __future__ import annotations

USER = {"X-User-Id": "alice@example.com"}

def test_list_empty(client):
    r = client.get("/api/v1/mcp-servers")
    assert r.status_code == 200
    assert r.json() == []

def test_create_stdio_minimal(client):
    r = client.post(
        "/api/v1/mcp-servers",
        headers=USER,
        json={
            "name": "filesystem",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "filesystem"
    assert body["transport"] == "stdio"
    assert body["enabled"] is True
    assert body["command"] == "npx"
    assert body["args"] == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    assert body["id"].startswith("mcp-")
    # The response carries the owner's id so the frontend can
    # disable edit/delete affordances for non-owned rows.
    assert body["userId"] == "alice@example.com"

def test_create_sse(client):
    r = client.post(
        "/api/v1/mcp-servers",
        headers=USER,
        json={
            "name": "remote",
            "transport": "sse",
            "url": "http://localhost:3000/sse",
            "headers": {"Authorization": "Bearer x"},
            "enabled": False,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["transport"] == "sse"
    assert body["url"] == "http://localhost:3000/sse"
    assert body["headers"] == {"Authorization": "Bearer x"}
    assert body["enabled"] is False

def test_create_with_explicit_id(client):
    r = client.post(
        "/api/v1/mcp-servers",
        headers=USER,
        json={
            "id": "my-fs",
            "name": "filesystem",
            "transport": "stdio",
            "command": "npx",
        },
    )
    assert r.status_code == 201
    assert r.json()["id"] == "my-fs"

def test_create_stdio_without_command_422(client):
    r = client.post(
        "/api/v1/mcp-servers",
        headers=USER,
        json={"name": "broken", "transport": "stdio"},
    )
    assert r.status_code == 422

def test_create_sse_without_url_422(client):
    r = client.post(
        "/api/v1/mcp-servers",
        headers=USER,
        json={"name": "broken", "transport": "sse"},
    )
    assert r.status_code == 422

def test_get(client):
    create = client.post(
        "/api/v1/mcp-servers",
        headers=USER,
        json={"name": "fs", "transport": "stdio", "command": "npx"},
    ).json()
    r = client.get(f"/api/v1/mcp-servers/{create['id']}", headers=USER)
    assert r.status_code == 200
    assert r.json()["id"] == create["id"]

def test_get_not_found_404(client):
    r = client.get("/api/v1/mcp-servers/does-not-exist", headers=USER)
    assert r.status_code == 404

def test_update_toggle_enabled(client):
    create = client.post(
        "/api/v1/mcp-servers",
        headers=USER,
        json={"name": "fs", "transport": "stdio", "command": "npx"},
    ).json()
    r = client.patch(
        f"/api/v1/mcp-servers/{create['id']}",
        headers=USER,
        json={"enabled": False},
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is False

def test_delete(client):
    create = client.post(
        "/api/v1/mcp-servers",
        headers=USER,
        json={"name": "fs", "transport": "stdio", "command": "npx"},
    ).json()
    r = client.delete(
        f"/api/v1/mcp-servers/{create['id']}",
        headers=USER,
    )
    assert r.status_code == 204
    # gone
    assert client.get(f"/api/v1/mcp-servers/{create['id']}").status_code == 404

def test_delete_not_found_404(client):
    r = client.delete("/api/v1/mcp-servers/does-not-exist", headers=USER)
    assert r.status_code == 404

def test_list_returns_multiple(client):
    for i in range(3):
        client.post(
            "/api/v1/mcp-servers",
            headers=USER,
            json={"name": f"fs-{i}", "transport": "stdio", "command": "npx"},
        )
    r = client.get("/api/v1/mcp-servers", headers=USER)
    assert r.status_code == 200
    assert len(r.json()) == 3