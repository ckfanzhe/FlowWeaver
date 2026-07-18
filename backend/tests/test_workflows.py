"""Workflow CRUD endpoint tests."""
from __future__ import annotations

def test_list_empty(client):
    r = client.get("/api/v1/workflows")
    assert r.status_code == 200
    assert r.json() == []

def test_create_minimal(client):
    r = client.post("/api/v1/workflows", json={"name": "My First"})
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "My First"
    assert body["description"] is None
    assert body["nodes"] == []
    assert body["edges"] == []
    assert body["id"].startswith("wf-")
    assert "createdAt" in body
    assert "updatedAt" in body

def test_create_with_nodes_and_edges(client):
    """A workflow with multiple agents wired together; no input/output
    nodes — the workflow's input comes from `Workflow.run(input=...)`
    and the output is the last Step's result.
    """
    payload = {
        "name": "Complex",
        "description": "Multi-node",
        "nodes": [
            {"id": "n1", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "Bot", "config": {"instructions": "be helpful"}}},
            {"id": "n2", "type": "agent", "position": {"x": 100, "y": 0},
             "data": {"label": "Bot2", "config": {"instructions": "be brief"}}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2"},
        ],
    }
    r = client.post("/api/v1/workflows", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert len(body["nodes"]) == 2
    assert len(body["edges"]) == 1
    assert body["nodes"][0]["data"]["config"]["instructions"] == "be helpful"
    assert body["edges"][0]["source"] == "n1"

def test_create_rejects_empty_name(client):
    r = client.post("/api/v1/workflows", json={"name": ""})
    assert r.status_code == 422

def test_create_rejects_unknown_node_type(client):
    r = client.post(
        "/api/v1/workflows",
        json={
            "name": "bad",
            "nodes": [{"id": "n1", "type": "wat", "position": {"x": 0, "y": 0}, "data": {}}],
        },
    )
    assert r.status_code == 422

# `WorkflowNode.type` is validated against the manifest registry
# (see `app.core.node_types.NODE_TYPES`) instead of a static
# `Literal[...]`. The schema is manifest-driven: every base type
# listed in the manifest is accepted, anything else is rejected
# with a 422. The tests below pin both directions.
def test_create_accepts_every_base_type(client):
    """Every base type in the manifest must be accepted on POST.

    The unified node-type set is: `tool` (covers http, mcp, and
    function tools), `flow` (covers parallel + steps), and `branch`
    (covers router + condition). The 5 preset types are also
    accepted as base manifest entries — `tool` extends naturally
    for the tool-backed ones."""
    base_types = (
        "agent", "tool", "branch", "flow", "loop", "human_input",
        "wikipedia", "tavily_search", "duckduckgo", "calculator", "arxiv_search",
    )
    for t in base_types:
        r = client.post(
            "/api/v1/workflows",
            json={
                "name": f"with-{t}",
                "nodes": [
                    {"id": "n1", "type": t, "position": {"x": 0, "y": 0}, "data": {}},
                ],
                "edges": [],
            },
        )
        assert r.status_code == 201, f"{t!r}: {r.text}"

def test_put_round_trip_with_mixed_types(client):
    """Regression for the autosave path: PUT a workflow carrying
    several different base types and confirm all of them survive.

    The legacy `http` and `mcp` node types are migrated to
    `tool` + `source` via `_compat` on read. The downstream
    `types` list reflects the post-migration shape."""
    create = client.post(
        "/api/v1/workflows",
        json={
            "name": "V1",
            "nodes": [
                {"id": "n1", "type": "agent", "position": {"x": 0, "y": 0}, "data": {}},
            ],
            "edges": [],
        },
    ).json()
    r = client.put(
        f"/api/v1/workflows/{create['id']}",
        json={
            "name": "V2",
            "nodes": [
                {"id": "a", "type": "agent", "position": {"x": 0, "y": 0}, "data": {}},
                {"id": "h", "type": "http", "position": {"x": 100, "y": 0}, "data": {}},
                {"id": "m", "type": "mcp", "position": {"x": 200, "y": 0}, "data": {}},
                {"id": "r", "type": "router", "position": {"x": 300, "y": 0}, "data": {}},
            ],
            "edges": [],
        },
    )
    assert r.status_code == 200, r.text
    # _compat migrates http→tool+source='http', mcp→tool+source='mcp',
    # router→branch on the read path. The persisted shape carries the
    # post-migration type names.
    types = sorted(n["type"] for n in r.json()["nodes"])
    assert types == ["agent", "branch", "tool", "tool"]

def test_workflow_round_trip_with_wikipedia_preset(client):
    """End-to-end: create a workflow carrying a wikipedia preset
    via `type='tool'` + `config.preset='wikipedia'`, GET it back,
    and verify the config round-trips intact.

    The wikipedia preset no longer has its
    own manifest entry — it routes through the unified `tool` node
    via the `preset` config discriminator. The legacy
    `type: 'wikipedia'` literal is auto-migrated to
    `type: 'tool'` + `preset: 'wikipedia'` on read by
    `_compat.migrate_node_dict`. The HTTP fields (`method`,
    `baseUrl`, `path`, ...) are still valid because the preset's
    `default_source='http'` forces them into scope via
    `PRESET_REGISTRY`.

    The manifest's `defaultConfig` is what the frontend reads via
    `/api/v1/node-types` and pre-populates when a new node is
    dropped — the backend doesn't auto-inject defaults into a
    freshly-created node. So this test sends an explicit config
    (matching the manifest defaults) and verifies it survives.
    """
    payload = {
        "name": "agent-with-wikipedia",
        "nodes": [
            {
                "id": "a1",
                "type": "agent",
                "position": {"x": 0, "y": 0},
                "data": {"label": "Researcher"},
            },
            {
                "id": "w1",
                "type": "tool",
                "position": {"x": 200, "y": 0},
                "data": {
                    "label": "Wikipedia",
                    "config": {
                        "preset": "wikipedia",
                        "source": "http",
                        "toolName": "wikipedia_search",
                        "toolDescription": "Search Wikipedia for articles matching a query",
                        "method": "GET",
                        "baseUrl": "https://en.wikipedia.org",
                        "path": "/w/api.php?action=query&list=search&srsearch={query}&format=json",
                        "headers": {},
                        "queryParams": {},
                        "authToken": "",
                        "bodySchema": "",
                    },
                },
            },
        ],
        "edges": [],
    }
    create = client.post("/api/v1/workflows", json=payload).json()
    got = client.get(f"/api/v1/workflows/{create['id']}").json()
    # Both nodes survived. The wikipedia preset collapsed into
    # the unified `tool` node — the persisted shape is `type='tool'`.
    assert {n["type"] for n in got["nodes"]} == {"agent", "tool"}
    # Wikipedia config survives the round-trip with the same shape
    # (validated against ToolNodeConfig with preset='wikipedia').
    wiki = next(n for n in got["nodes"] if n["type"] == "tool")
    cfg = wiki["data"]["config"]
    assert cfg["preset"] == "wikipedia"
    assert cfg["toolName"] == "wikipedia_search"
    assert cfg["baseUrl"] == "https://en.wikipedia.org"
    assert "{query}" in cfg["path"]

def test_workflow_accepts_wikipedia_with_minimal_config(client):
    """Wikipedia now shares the merged
    `ToolNodeConfig` schema (with `source: 'http'` discriminator),
    so a minimal config (just toolName + baseUrl) must validate
    and round-trip. The `source` defaults to `function` on bare
    ToolNodeConfig — wikipedia's preset-supplied default is `http`."""
    payload = {
        "name": "minimal-wikipedia",
        "nodes": [
            {
                "id": "w1",
                "type": "wikipedia",
                "position": {"x": 0, "y": 0},
                "data": {
                    "label": "Wikipedia",
                    "config": {
                        "source": "http",
                        "toolName": "wikipedia_search",
                        "toolDescription": "Search Wikipedia",
                        "baseUrl": "https://en.wikipedia.org",
                    },
                },
            },
        ],
        "edges": [],
    }
    create = client.post("/api/v1/workflows", json=payload).json()
    got = client.get(f"/api/v1/workflows/{create['id']}").json()
    assert got["nodes"][0]["data"]["config"]["toolName"] == "wikipedia_search"

def test_get(client):
    create = client.post("/api/v1/workflows", json={"name": "X"}).json()
    r = client.get(f"/api/v1/workflows/{create['id']}")
    assert r.status_code == 200
    assert r.json()["id"] == create["id"]

def test_get_not_found_404(client):
    r = client.get("/api/v1/workflows/does-not-exist")
    assert r.status_code == 404

def test_put_replaces_full(client):
    # Create a valid workflow first (single agent), then PUT replaces it
    # with a different valid shape.
    create = client.post(
        "/api/v1/workflows",
        json={
            "name": "V1",
            "nodes": [
                {"id": "n1", "type": "agent", "position": {"x": 0, "y": 0}, "data": {}},
            ],
            "edges": [],
        },
    ).json()
    r = client.put(
        f"/api/v1/workflows/{create['id']}",
        json={
            "name": "V2",
            "description": "renamed",
            "nodes": [
                {"id": "a", "type": "agent", "position": {"x": 0, "y": 0}, "data": {}},
            ],
            "edges": [],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "V2"
    assert body["description"] == "renamed"
    assert len(body["nodes"]) == 1
    assert len(body["edges"]) == 0

def test_patch_updates_only_name(client):
    # Create a valid workflow first (single agent); PATCH only the name.
    create = client.post(
        "/api/v1/workflows",
        json={
            "name": "V1",
            "nodes": [
                {"id": "n1", "type": "agent", "position": {"x": 0, "y": 0}, "data": {}},
            ],
            "edges": [],
        },
    ).json()
    r = client.patch(f"/api/v1/workflows/{create['id']}", json={"name": "V1b"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "V1b"
    # nodes untouched
    assert len(body["nodes"]) == 1

def test_patch_updates_nodes_only(client):
    # PATCH can replace nodes/edges together; we update both so the
    # post-PATCH state remains a valid workflow.
    create = client.post(
        "/api/v1/workflows",
        json={"name": "V1"},
    ).json()
    r = client.patch(
        f"/api/v1/workflows/{create['id']}",
        json={
            "nodes": [
                {"id": "x", "type": "agent", "position": {"x": 1, "y": 2}, "data": {}},
            ],
            "edges": [],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "V1"  # unchanged
    assert len(body["nodes"]) == 1
    assert body["nodes"][0]["position"] == {"x": 1, "y": 2}

def test_delete(client):
    create = client.post("/api/v1/workflows", json={"name": "X"}).json()
    r = client.delete(f"/api/v1/workflows/{create['id']}")
    assert r.status_code == 204
    assert client.get(f"/api/v1/workflows/{create['id']}").status_code == 404

def test_delete_not_found_404(client):
    r = client.delete("/api/v1/workflows/does-not-exist")
    assert r.status_code == 404

def test_list_returns_multiple_in_updated_desc(client):
    ids = []
    for i in range(3):
        ids.append(client.post("/api/v1/workflows", json={"name": f"w{i}"}).json()["id"])
    r = client.get("/api/v1/workflows")
    assert r.status_code == 200
    names = [w["name"] for w in r.json()]
    # most recently updated first
    assert names == ["w2", "w1", "w0"]
