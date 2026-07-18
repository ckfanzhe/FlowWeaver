"""Tests for JSON workflow import/export — sharing flows.

Covers:
  - export-json returns a valid envelope with the right shape
  - import-json creates a new workflow row
  - malformed payloads are rejected with 422
  - round-trip (export → import → export) preserves the workflow
  - duplicate node ids, dangling edge endpoints, cycles are caught
"""
from __future__ import annotations

import json

SAMPLE_WF = {
    "name": "Test Flow",
    "description": "smoke test",
    "nodes": [
        {"id": "ag1", "type": "agent", "position": {"x": 0, "y": 0},
         "data": {"label": "Bot1", "config": {"instructions": "be helpful"}}},
        {"id": "ag2", "type": "agent", "position": {"x": 100, "y": 0},
         "data": {"label": "Bot2", "config": {}}},
    ],
    "edges": [
        {"id": "e1", "source": "ag1", "target": "ag2"},
    ],
}

def _create_workflow(client) -> str:
    r = client.post("/api/v1/workflows", json=SAMPLE_WF)
    assert r.status_code == 201, r.text
    return r.json()["id"]

# ─────────────────────────────────────────────────────────────────
# export-json
# ─────────────────────────────────────────────────────────────────
def test_export_json_returns_envelope(client):
    wf_id = _create_workflow(client)
    r = client.get(f"/api/v1/workflows/{wf_id}/export-json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert ".json" in cd

    envelope = r.json()
    assert envelope["schemaVersion"].startswith("1.")
    assert envelope["kind"] == "agnobuilder.workflow"
    assert "exportedAt" in envelope
    body = envelope["workflow"]
    assert body["name"] == "Test Flow"
    assert body["description"] == "smoke test"
    assert len(body["nodes"]) == 2
    assert len(body["edges"]) == 1
    # ensure no internal leakage
    assert "id" not in body
    assert "createdAt" not in body

def test_export_json_404_for_unknown_workflow(client):
    r = client.get("/api/v1/workflows/no-such/export-json")
    assert r.status_code == 404

# ─────────────────────────────────────────────────────────────────
# import-json
# ─────────────────────────────────────────────────────────────────
def test_import_json_creates_new_workflow(client):
    envelope = {
        "schemaVersion": "1.0",
        "kind": "agnobuilder.workflow",
        "exportedAt": "2026-08-12T00:00:00+00:00",
        "workflow": SAMPLE_WF,
    }
    r = client.post("/api/v1/workflows/import-json", json={"payload": envelope})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Test Flow"
    assert len(body["nodes"]) == 2
    assert len(body["edges"]) == 1
    # a NEW id was generated
    assert body["id"].startswith("wf-")

def test_imported_workflow_is_listed_in_workflows(client):
    envelope = {
        "schemaVersion": "1.0",
        "kind": "agnobuilder.workflow",
        "workflow": SAMPLE_WF,
    }
    client.post("/api/v1/workflows/import-json", json={"payload": envelope})
    r = client.get("/api/v1/workflows")
    names = [w["name"] for w in r.json()]
    assert "Test Flow" in names

# ─────────────────────────────────────────────────────────────────
# Round-trip
# ─────────────────────────────────────────────────────────────────
def test_round_trip_export_then_import_preserves_workflow(client):
    """Export → import → export the new one — both JSONs should match
    (ignoring exportedAt timestamps)."""
    orig_id = _create_workflow(client)
    r1 = client.get(f"/api/v1/workflows/{orig_id}/export-json")
    env1 = r1.json()

    r2 = client.post("/api/v1/workflows/import-json", json={"payload": env1})
    new_id = r2.json()["id"]
    assert new_id != orig_id

    r3 = client.get(f"/api/v1/workflows/{new_id}/export-json")
    env2 = r3.json()
    # compare the workflow body only — timestamps will differ
    assert env1["workflow"] == env2["workflow"]

# ─────────────────────────────────────────────────────────────────
# Validation — every error path returns 422
# ─────────────────────────────────────────────────────────────────
def _post(envelope: dict):
    return None  # placeholder so the editor can use _post later

def test_import_rejects_non_object_payload(client):
    r = client.post("/api/v1/workflows/import-json", json={"payload": "not-a-dict"})
    assert r.status_code == 422

def test_import_rejects_wrong_kind(client):
    r = client.post("/api/v1/workflows/import-json", json={
        "payload": {"kind": "something.else", "workflow": SAMPLE_WF, "schemaVersion": "1.0"},
    })
    assert r.status_code == 422
    assert "kind" in r.json()["detail"].lower()

def test_import_rejects_unsupported_version(client):
    r = client.post("/api/v1/workflows/import-json", json={
        "payload": {"kind": "agnobuilder.workflow", "schemaVersion": "99.0", "workflow": SAMPLE_WF},
    })
    assert r.status_code == 422
    assert "version" in r.json()["detail"].lower()

def test_import_rejects_empty_workflow(client):
    r = client.post("/api/v1/workflows/import-json", json={
        "payload": {"kind": "agnobuilder.workflow", "schemaVersion": "1.0",
                    "workflow": {"name": "x", "nodes": [], "edges": []}},
    })
    assert r.status_code == 422
    assert "empty" in r.json()["detail"].lower()

def test_import_rejects_duplicate_node_ids(client):
    bad = {
        "kind": "agnobuilder.workflow",
        "schemaVersion": "1.0",
        "workflow": {
            "name": "dup",
            "nodes": [
                {"id": "a", "type": "agent", "position": {"x": 0, "y": 0}, "data": {}},
                {"id": "a", "type": "agent", "position": {"x": 1, "y": 0}, "data": {}},
            ],
            "edges": [],
        },
    }
    r = client.post("/api/v1/workflows/import-json", json={"payload": bad})
    assert r.status_code == 422
    # The duplicate-node-id error can surface as either a plain string
    # (`parse_envelope` failure) or a structured `detail.errors` dict
    # (the connection-rules pre-flight). Both are valid 422 paths.
    body = r.json()["detail"]
    if isinstance(body, dict):
        msg = body.get("message", "")
        codes = [e.get("code", "") for e in body.get("errors", [])]
        assert "duplicateNodeId" in codes or "duplicate" in msg.lower()
    else:
        assert "duplicate" in body.lower()

def test_import_rejects_dangling_edge(client):
    bad = {
        "kind": "agnobuilder.workflow",
        "schemaVersion": "1.0",
        "workflow": {
            "name": "dangling",
            "nodes": [
                {"id": "a1", "type": "agent", "position": {"x": 0, "y": 0}, "data": {}},
            ],
            "edges": [
                {"id": "e", "source": "a1", "target": "ghost"},
            ],
        },
    }
    r = client.post("/api/v1/workflows/import-json", json={"payload": bad})
    assert r.status_code == 422

def test_import_rejects_missing_name(client):
    bad = {
        "kind": "agnobuilder.workflow",
        "schemaVersion": "1.0",
        "workflow": {"nodes": SAMPLE_WF["nodes"], "edges": SAMPLE_WF["edges"]},
    }
    r = client.post("/api/v1/workflows/import-json", json={"payload": bad})
    assert r.status_code == 422
    assert "name" in r.json()["detail"].lower()

def test_import_rejects_cycle(client):
    bad = {
        "kind": "agnobuilder.workflow",
        "schemaVersion": "1.0",
        "workflow": {
            "name": "cycle",
            "nodes": [
                {"id": "a", "type": "agent", "position": {"x": 0, "y": 0}, "data": {}},
                {"id": "b", "type": "agent", "position": {"x": 1, "y": 0}, "data": {}},
            ],
            "edges": [
                {"id": "e1", "source": "a", "target": "b"},
                {"id": "e2", "source": "b", "target": "a"},
            ],
        },
    }
    r = client.post("/api/v1/workflows/import-json", json={"payload": bad})
    assert r.status_code == 422
