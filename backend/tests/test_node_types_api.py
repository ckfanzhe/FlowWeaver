"""API tests for `/api/v1/node-types`.

Pins the response shape: every entry must surface `kind`, `extends`,
`ui`, `capabilities`, and the resolved `defaultConfig` so the
frontend can read everything it needs from a single fetch.
"""
from __future__ import annotations

import pytest

@pytest.fixture
def client():
    """A FastAPI test client backed by an in-memory SQLite session.

    Avoids the lifespan hook seeding real rows — we only need the
    endpoint to be reachable. The session fixture from
    `test_templates_api` lives in the same module family; we keep
    this one minimal to stay focused on the response shape.
    """
    from fastapi.testclient import TestClient

    from app.main import app
    return TestClient(app)

def test_node_types_endpoint_reachable(client):
    r = client.get("/api/v1/node-types")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schemaVersion"] == 2
    assert "types" in body and isinstance(body["types"], list)
    assert "entries" in body and isinstance(body["entries"], dict)

def test_node_types_endpoint_lists_every_manifest_entry(client):
    r = client.get("/api/v1/node-types")
    body = r.json()
    # There are 7 base types — agent, branch (collapsed from the
    # earlier router/condition pair), flow (collapsed from
    # parallel/steps), loop, ask (renamed from human_input), tool
    # (collapsed from http/mcp/tools plus the 5 preset tool types),
    # and knowledge (RAG / vector DB source — new in
    # [[gleaming-munching-grove]]). Asserted as exact count so a
    # future base-type addition is a deliberate test update rather
    # than a silent drift. The 5 presets now route through the
    # `tool` node's `preset` config discriminator and no longer
    # appear as separate entries in the manifest.
    assert len(body["types"]) == 7
    assert set(body["types"]) == {
        "agent", "branch", "flow", "loop", "ask", "tool", "knowledge",
    }

def test_node_types_endpoint_includes_phase7_fields(client):
    """Every entry surfaces the structural fields: `kind`, `extends`,
    `ui`, `capabilities`, `defaultConfig`."""
    r = client.get("/api/v1/node-types")
    body = r.json()
    for name, entry in body["entries"].items():
        # Structural fields — must be present and well-typed.
        assert "kind" in entry, f"{name} missing kind"
        assert entry["kind"] in (
            "executable", "compound", "tool_source", "knowledge_source", "control_flow",
        )
        assert "extends" in entry, f"{name} missing extends"
        assert "ui" in entry, f"{name} missing ui"
        assert set(entry["ui"].keys()) == {"group", "form", "paletteOrder"}
        assert "capabilities" in entry, f"{name} missing capabilities"
        assert set(entry["capabilities"].keys()) == {
            "compoundPass", "isToolSource", "isKnowledgeSource",
            "needsToolWiring", "needsKnowledgeWiring",
            "skipPass1", "stepWrapper",
        }
        assert "defaultConfig" in entry, f"{name} missing defaultConfig"
        assert isinstance(entry["defaultConfig"], dict)

def test_node_types_endpoint_compound_pass_ordering(client):
    """Each compound type carries a non-null `compoundPass`
    integer matching the manifest."""
    r = client.get("/api/v1/node-types")
    body = r.json()
    expected = {"flow": 10, "branch": 20, "loop": 30}
    for ntype, expected_pass in expected.items():
        assert body["entries"][ntype]["capabilities"]["compoundPass"] == expected_pass

def test_node_types_endpoint_step_wrapper_per_kind(client):
    """`agent` and `ask` are the only types with a
    non-`"none"` `stepWrapper`."""
    r = client.get("/api/v1/node-types")
    body = r.json()
    assert body["entries"]["agent"]["capabilities"]["stepWrapper"] == "agent"
    assert body["entries"]["ask"]["capabilities"]["stepWrapper"] == "ask"
    # The three tool-source types (http/mcp/tools) collapsed into one
    # `tool` entry — it joins the stepWrapper='none' group below.
    # The 5 preset tool types collapsed into the `tool` node's
    # `preset` config discriminator — no separate preset entries
    # exist in the manifest anymore. Knowledge sources (new in
    # [[gleaming-munching-grove]]) are not in `wf.steps` either, so
    # they also live in the `stepWrapper='none'` group.
    for ntype in ("branch", "flow", "loop", "tool", "knowledge"):
        assert body["entries"][ntype]["capabilities"]["stepWrapper"] == "none", (
            f"{ntype} should have stepWrapper=none"
        )

def test_node_types_endpoint_tool_preset_discriminator(client):
    """The wikipedia preset no longer
    appears as a separate manifest entry — it routes through the
    unified `tool` node's `preset` config discriminator. The preset
    defaults (HTTP wrapper against en.wikipedia.org) live in
    `app.core.strategies.tool.PRESET_REGISTRY` and get prefilled
    at runtime by `_apply_preset_defaults`. The `tool` entry's
    defaultConfig carries `preset=null` (plain tool) so the LLM
    knows to set it explicitly when asked for a preset tool."""
    r = client.get("/api/v1/node-types")
    body = r.json()
    entry = body["entries"]["tool"]
    assert "wikipedia" not in body["entries"], (
        "wikipedia preset no longer exists as a separate manifest entry "
        "— it collapsed into the `tool` node's `preset` discriminator"
    )
    # The tool entry's defaultConfig carries the new `preset` field.
    assert "preset" in entry["defaultConfig"]
    assert entry["defaultConfig"]["preset"] is None
    # Default `source` is `function` (no-op empty-functions source)
    # so a freshly-dropped `tool` node is inert until the user picks
    # a different mode or a preset. Switching `source` to `http`
    # / `mcp` (or setting `preset`) is what makes it emit a wrapper.
    assert entry["defaultConfig"]["source"] == "function"
    assert entry["defaultConfig"]["toolName"] == ""
    assert entry["defaultConfig"]["functions"] == []