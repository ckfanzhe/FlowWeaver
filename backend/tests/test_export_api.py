"""Tests for the `/api/v1/workflows/{id}/export` endpoint.

The endpoint returns a downloadable Python file representing the workflow.
"""
from __future__ import annotations

import ast

def _create_workflow(client, **overrides):
    payload = {
        "name": "export-demo",
        "nodes": [
            {"id": "n2", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A",
                      "config": {"model": {"provider": "openai", "modelId": "gpt-4o",
                                            "apiKey": "sk-test"},
                                 "instructions": "hello"}}},
        ],
        "edges": [],
    }
    payload.update(overrides)
    r = client.post("/api/v1/workflows", json=payload)
    assert r.status_code in (200, 201), r.text
    return r.json()

def test_export_returns_python_source(client):
    wf = _create_workflow(client)
    r = client.get(f"/api/v1/workflows/{wf['id']}/export")
    assert r.status_code == 200
    # the body must be valid Python
    ast.parse(r.text)

def test_export_content_type_is_python(client):
    wf = _create_workflow(client)
    r = client.get(f"/api/v1/workflows/{wf['id']}/export")
    # text/x-python is the canonical mime for .py downloads
    ct = r.headers.get("content-type", "")
    assert "python" in ct or "text/plain" in ct

def test_export_suggests_filename(client):
    wf = _create_workflow(client)
    r = client.get(f"/api/v1/workflows/{wf['id']}/export")
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert ".py" in cd

def test_export_404_for_unknown_workflow(client):
    r = client.get("/api/v1/workflows/wf-nope/export")
    assert r.status_code == 404

def test_export_body_contains_workflow_name(client):
    wf = _create_workflow(client)
    r = client.get(f"/api/v1/workflows/{wf['id']}/export")
    assert "export_demo" in r.text  # safe_name lowercases + underscores