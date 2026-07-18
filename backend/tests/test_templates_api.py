"""Tests for the built-in templates API: list, get, instantiate, and 403 guards."""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy.orm import sessionmaker

from app.db import session as session_module
from app.db.models import Workflow  # noqa: F401
from app.db.session import session_scope  # noqa: F401
from app.main import _seed_templates

# ─────────────────────────────────────────────────────────────────
# Local fixtures
# ─────────────────────────────────────────────────────────────────
@pytest.fixture()
def seeded(engine):
    """Seed the built-in templates into the in-memory test DB.

    The FastAPI `lifespan` hook runs `_seed_templates()` but only against
    the production (file-backed) DB — and `AGNOBUILDER_SKIP_SEED=1` is
    set in conftest so even that wouldn't fire. We call the seed
    explicitly here, with a patched `session_scope` that binds to the
    test engine.
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

def test_seed_via_test_engine(engine, seeded):
    """Sanity: the fixture actually inserted the built-in templates into the test DB."""
    from app.templates_loader import discover_templates
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as s:
        n = s.query(Workflow).filter_by(is_template=True).count()
    expected = len(discover_templates())
    assert n == expected, (
        f"expected {expected} templates (matching discover_templates), got {n}"
    )

# ─────────────────────────────────────────────────────────────────
# Listing
# ─────────────────────────────────────────────────────────────────
def test_list_templates_returns_all_seeded(client, seeded):
    from app.templates_loader import discover_templates
    r = client.get("/api/v1/workflows/templates")
    assert r.status_code == 200
    body = r.json()
    expected = len(discover_templates())
    assert len(body) == expected, (
        f"expected {expected} built-in templates, got {len(body)}"
    )
    # All rows are flagged as templates
    assert all(t["isTemplate"] is True for t in body)
    # Names are the human-readable ones, not the slug ids. The 5
    # built-ins after the templates-5 cleanup each cover one concept:
    # agent / ask / branch / loop+ask / flow+aggregation.
    names = {t["name"] for t in body}
    assert "Hello World" in names
    assert "Ask the User" in names
    assert "Conditional Greeting" in names
    assert "Iterative Story Generator" in names
    assert "Parallel Research with Synthesis" in names

def test_template_summary_includes_node_types_and_counts(client, seeded):
    r = client.get("/api/v1/workflows/templates")
    assert r.status_code == 200
    by_id = {t["id"]: t for t in r.json()}

    # Hello World: a single agent, 1 node, 0 edges. The workflow's input
    # comes from `Workflow.run(input=...)` and the output is the agent's
    # result — no dedicated input/output nodes.
    hw = by_id["tpl-hello-world"]
    assert hw["nodeCount"] == 1
    assert hw["edgeCount"] == 0
    assert hw["nodeTypes"] == ["agent"]
    assert hw["category"] == "starter"

    # Conditional Greeting: 1 branch + 2 responders = 3 nodes / 2 edges.
    # Branch-node collapse: the template carries `type: "branch"`
    # (the prior `router` template was migrated on read).
    cg = by_id["tpl-conditional-greeting"]
    assert cg["nodeCount"] == 3
    assert cg["edgeCount"] == 2
    assert set(cg["nodeTypes"]) == {"branch", "agent"}

    # Iterative Story: covers the `loop` node type (a self-iterating
    # writer that loops until it outputs 'DONE').
    ist = by_id["tpl-iterative-story"]
    assert "loop" in ist["nodeTypes"]

def test_templates_cover_agent_ask_branch_flow_loop(client, seeded):
    """Every non-tool node type should appear in at least one template.

    The 5 base node types are demonstrated across the 5 built-in
    templates: `agent` (hello-world / ask-the-user / conditional-greeting
    / iterative-story / parallel-summary), `ask` (ask-the-user),
    `branch` (conditional-greeting), `flow` (parallel-summary),
    `loop` (iterative-story).

    `tool` is intentionally NOT covered by any template — the node
    type is discoverable via the manifest schema + the property panel
    tool picker, but adding a tool node to one of the 5 would dilute
    its single-concept focus (per the "concise, clean, focused,
    pedagogically meaningful" audit principle).

    `mcp` is deferred — requires a configured `McpServer` row and the
    first-run UX isn't gentle enough yet.

    `input` and `output` are NOT node types: the workflow's input comes
    from `Workflow.run(input=...)` and the output is the last Step's
    result.
    """
    r = client.get("/api/v1/workflows/templates")
    seen: set[str] = set()
    for t in r.json():
        seen.update(t["nodeTypes"])
    # `parallel`+`steps` collapsed into `flow`,
    # `router`+`condition` collapsed into `branch`,
    # `http`+`mcp`+`tools` collapsed into `tool`.
    # Templates were migrated to the unified type strings when
    # the templates gallery was trimmed; the templates API exposes
    # only the post-merge names.
    expected = {
        "agent", "ask",
        "branch", "flow", "loop",
    }
    missing = expected - seen
    assert not missing, f"templates missing node types: {missing}"
    # http / mcp / tools / parallel / steps / router / condition
    # are all legacy type strings; templates now use the unified
    # names.
    for legacy in ("http", "mcp", "tools", "parallel", "steps",
                   "router", "condition"):
        assert legacy not in seen, (
            f"templates still expose legacy type {legacy!r} — "
            f"the migration to the unified shape was incomplete"
        )
    # input/output are not node types at all
    assert "input" not in seen
    assert "output" not in seen

# ─────────────────────────────────────────────────────────────────
# Default scope filter — user workflow should NOT return templates
# ─────────────────────────────────────────────────────────────────
def test_default_list_excludes_templates(client, seeded):
    # Seed a user workflow on top of the templates.
    client.post("/api/v1/workflows", json={"name": "My User Workflow"})
    r = client.get("/api/v1/workflows")
    assert r.status_code == 200
    body = r.json()
    names = [w["name"] for w in body]
    assert "My User Workflow" in names
    # No template rows should appear in the default view.
    assert "Hello World" not in names
    assert all(w["isTemplate"] is False for w in body)

def test_list_scope_user_excludes_templates(client, seeded):
    client.post("/api/v1/workflows", json={"name": "U1"})
    r = client.get("/api/v1/workflows?scope=user")
    assert all(w["isTemplate"] is False for w in r.json())

def test_list_scope_templates_excludes_user_workflows(client, seeded):
    client.post("/api/v1/workflows", json={"name": "U1"})
    r = client.get("/api/v1/workflows?scope=templates")
    body = r.json()
    assert all(w["isTemplate"] is True for w in body)
    assert "U1" not in {w["name"] for w in body}

def test_list_scope_all_returns_everything(client, seeded):
    client.post("/api/v1/workflows", json={"name": "U1"})
    r = client.get("/api/v1/workflows?scope=all")
    body = r.json()
    assert any(w["name"] == "U1" for w in body)
    assert any(w["isTemplate"] is True for w in body)

def test_list_scope_rejects_unknown_value(client):
    r = client.get("/api/v1/workflows?scope=admin")
    assert r.status_code == 422

# ─────────────────────────────────────────────────────────────────
# Get full template
# ─────────────────────────────────────────────────────────────────
def test_get_template_returns_full_workflow(client, seeded):
    r = client.get("/api/v1/workflows/templates/tpl-hello-world")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "tpl-hello-world"
    assert body["isTemplate"] is True
    # Full nodes + edges — Hello World is just a single agent.
    assert len(body["nodes"]) == 1
    assert len(body["edges"]) == 0
    node_types = {n["type"] for n in body["nodes"]}
    assert node_types == {"agent"}

def test_get_template_404_on_unknown_id(client, seeded):
    r = client.get("/api/v1/workflows/templates/tpl-does-not-exist")
    assert r.status_code == 404

def test_get_template_404_on_user_workflow_id(client, seeded):
    """A user workflow id must not be reachable via the templates route."""
    user = client.post("/api/v1/workflows", json={"name": "U"}).json()
    r = client.get(f"/api/v1/workflows/templates/{user['id']}")
    assert r.status_code == 404

def test_get_workflow_route_still_returns_templates(client, seeded):
    """The generic `GET /workflows/{id}` route must still work for both
    user workflows AND templates (it doesn't filter by is_template)."""
    r = client.get("/api/v1/workflows/tpl-hello-world")
    assert r.status_code == 200
    assert r.json()["isTemplate"] is True

# ─────────────────────────────────────────────────────────────────
# Instantiate
# ─────────────────────────────────────────────────────────────────
def test_instantiate_template_creates_user_workflow(client, seeded):
    r = client.post("/api/v1/workflows/from-template/tpl-hello-world")
    assert r.status_code == 201
    body = r.json()
    # New id, not the template id.
    assert body["id"] != "tpl-hello-world"
    assert body["id"].startswith("wf-")
    # is_template false, category cleared.
    assert body["isTemplate"] is False
    assert body["category"] is None
    # Name suffixed with "(copy)".
    assert body["name"] == "Hello World (copy)"
    # Same graph contents.
    assert len(body["nodes"]) == 1
    assert len(body["edges"]) == 0

def test_instantiate_clone_is_independent(client, seeded):
    """Mutating the clone must not affect the original template's nodes."""
    clone = client.post("/api/v1/workflows/from-template/tpl-hello-world").json()
    # PATCH the clone with a new node (use 'agent' — 'output' is not a node type).
    new_nodes = clone["nodes"] + [
        {"id": "extra", "type": "agent", "position": {"x": 0, "y": 0}, "data": {}}
    ]
    r = client.patch(
        f"/api/v1/workflows/{clone['id']}",
        json={"nodes": new_nodes},
    )
    assert r.status_code == 200
    # Template still has 1 node.
    template = client.get("/api/v1/workflows/templates/tpl-hello-world").json()
    assert len(template["nodes"]) == 1

def test_instantiate_404_on_unknown_id(client, seeded):
    r = client.post("/api/v1/workflows/from-template/tpl-nope")
    assert r.status_code == 404

def test_instantiate_404_on_user_workflow_id(client, seeded):
    user = client.post("/api/v1/workflows", json={"name": "U"}).json()
    r = client.post(f"/api/v1/workflows/from-template/{user['id']}")
    assert r.status_code == 404

# ─────────────────────────────────────────────────────────────────
# Self-heal: rows whose content has drifted from the JSON source get
# rewritten on next seed. This guards against the failure mode where
# an older version's overzealous migration stripped all edges from
# every template and the seed (which used to skip when `existing > 0`)
# never re-populated them.
# ─────────────────────────────────────────────────────────────────
def test_seed_self_heals_corrupted_template_rows(engine, seeded):
    """Strip a template's edges/nodes directly in the DB (simulating
    the corruption caused by an older migration bug) and verify the
    next `_seed_templates()` call restores the row to match the JSON
    declaration — without affecting other (uncorrupted) rows."""
    from app.templates_loader import find_template
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    # Pick a template that has edges so the corruption is detectable.
    target_id = "tpl-parallel-summary"
    expected = find_template(target_id)
    assert expected is not None, f"{target_id} not found in templates folder"

    # Sanity: the seeded fixture just put a healthy row there.
    with SessionLocal() as s:
        row = s.query(Workflow).filter_by(id=target_id).one()
        before_edges = list(row.edges or [])
        assert len(before_edges) > 0, "fixture must have populated edges"

    # Corrupt it: drop edges and nodes entirely.
    with SessionLocal() as s:
        row = s.query(Workflow).filter_by(id=target_id).one()
        row.nodes = []
        row.edges = []
        s.commit()

    # Re-seed (must monkey-patch session_scope again because the
    # `seeded` fixture only patched for the original call).
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

    # The corrupted row is now restored.
    with SessionLocal() as s:
        row = s.query(Workflow).filter_by(id=target_id).one()
        assert len(row.nodes) == len(expected["envelope"]["workflow"]["nodes"])
        assert len(row.edges) == len(expected["envelope"]["workflow"]["edges"])
        # Edges content matches the in-code declaration (sample of e2):
        target_edges = {
            (e["source"], e["target"]) for e in expected["envelope"]["workflow"]["edges"]
        }
        actual_edges = {(e["source"], e["target"]) for e in row.edges}
        assert actual_edges == target_edges

    # And the row stays reachable via the public API (id stable).
    # `client` fixture is auto-injected; assert via a fresh request
    # would require the same scope patches — skip; the DB-level
    # assertion above already covers the integrity guarantee.

# ─────────────────────────────────────────────────────────────────
# 403 guards — templates are read-only via the public API
# ─────────────────────────────────────────────────────────────────
def test_put_template_returns_403(client, seeded):
    r = client.put(
        "/api/v1/workflows/tpl-hello-world",
        json={"name": "hacked"},
    )
    assert r.status_code == 403
    # Body still intact.
    body = client.get("/api/v1/workflows/tpl-hello-world").json()
    assert body["name"] == "Hello World"

def test_patch_template_returns_403(client, seeded):
    r = client.patch(
        "/api/v1/workflows/tpl-hello-world",
        json={"name": "hacked"},
    )
    assert r.status_code == 403

def test_delete_template_returns_403(client, seeded):
    r = client.delete("/api/v1/workflows/tpl-hello-world")
    assert r.status_code == 403
    # Still exists.
    assert client.get(
        "/api/v1/workflows/tpl-hello-world"
    ).status_code == 200

# ─────────────────────────────────────────────────────────────────
# Idempotent seed
# ─────────────────────────────────────────────────────────────────
def test_seed_is_idempotent(engine):
    """Running the seed twice must not duplicate rows."""
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
        _seed_templates()
        _seed_templates()
    finally:
        session_module.session_scope = original

    SessionLocal2 = sessionmaker(bind=engine)
    with SessionLocal2() as s:
        templates = s.query(Workflow).filter_by(is_template=True).count()
    from app.templates_loader import discover_templates
    assert templates == len(discover_templates())

# ─────────────────────────────────────────────────────────────────
# Connection-rules compliance — every built-in template must pass
# `validate_connections` after seeding.
# ─────────────────────────────────────────────────────────────────
def test_templates_pass_connection_rules(engine, seeded):
    """After seeding, every template's stored nodes+edges must satisfy
    the connection-rules validator. The migration step in
    `_seed_templates` should already have removed any historical
    violations; this test is the guard that future template additions
    stay clean."""
    from app.core.connection_rules import validate_connections
    from app.templates_loader import discover_templates

    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as s:
        rows = s.query(Workflow).filter_by(is_template=True).all()
        expected = len(discover_templates())
        assert len(rows) == expected, (
            f"expected {expected} templates, got {len(rows)}"
        )
        violations: dict[str, list[str]] = {}
        for row in rows:
            errors = validate_connections(list(row.nodes or []), list(row.edges or []))
            if errors:
                violations[row.id] = [e.message for e in errors]
    assert not violations, (
        f"templates fail connection rules: {violations}"
    )

# ─────────────────────────────────────────────────────────────────
# Migration: pre-existing violating rows must be auto-repaired in
# place by `_migrate_template_edges`, not just fresh-seeded ones.
#
# Direct-call update: this test now calls `_migrate_template_edges`
# DIRECTLY (bypassing `_seed_templates`). The seed used to leave
# orphans alone — now it deletes them — so the synthetic test row
# would be wiped before the migration assertions could run. Calling
# the migration function directly preserves the coverage of the
# legacy-edge / legacy-node cleanup paths. Orphan deletion itself is
# exercised separately by `test_seed_deletes_orphan_template_rows`
# below.
# ─────────────────────────────────────────────────────────────────
def test_migrate_removes_tool_source_edge_from_existing_template(engine):
    """Simulate an upgrade scenario: a template row already exists
    in the DB with a tool-source edge (the pre-fix state). The
    `_migrate_template_edges` helper must remove the bad edge on
    startup so the row passes the validator."""
    from app.main import _migrate_template_edges

    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    # Hand-craft a template row that violates the new rules. We use
    # a private id so it doesn't collide with the seeded gallery.
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
        from app.db.models import Workflow

        bad_nodes = [
            # Tool-source collapse: the old `http` type is now
            # `tool` with `source='http'`. The legacy type still
            # exists in the JSON to verify migration handles it.
            {"id": "h", "type": "tool", "position": {"x": 0, "y": 0},
             "data": {"config": {"source": "http", "toolName": "x"}}},
            {"id": "a", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"config": {"toolsRef": ["h"]}}},
            {"id": "i", "type": "input", "position": {"x": 0, "y": 0}, "data": {}},
            {"id": "o", "type": "output", "position": {"x": 0, "y": 0}, "data": {}},
        ]
        bad_edges = [
            {"id": "e1", "source": "i", "target": "a"},
            {"id": "e2", "source": "h", "target": "a"},     # ← tool-source
            {"id": "e3", "source": "a", "target": "o"},
        ]
        with SessionLocal() as s:
            s.add(Workflow(
                id="tpl-bad-pre-migration",
                name="Bad",
                description="Simulates an old template row.",
                nodes=bad_nodes,
                edges=bad_edges,
                is_template=True,
                category="starter",
            ))
            s.commit()

        # Run the migration DIRECTLY (see comment above). It must:
        #   - drop the `h → a` edge (tool-source), and
        #   - drop the legacy input/output nodes + their edges.
        # Commit the session so the row mutations are visible to the
        # next read.
        with SessionLocal() as s:
            _migrate_template_edges(s)
            s.commit()

        with SessionLocal() as s:
            row = s.query(Workflow).filter_by(id="tpl-bad-pre-migration").one()
            edge_sources = [e.get("source") for e in (row.edges or [])]
            edge_targets = [e.get("target") for e in (row.edges or [])]
            assert "h" not in edge_sources, (
                f"migration did not remove tool-source edge; "
                f"remaining sources: {edge_sources}"
            )
            # Legacy input/output nodes + their edges should be gone.
            assert "i" not in edge_targets, (
                f"migration did not remove input node edge; "
                f"remaining targets: {edge_targets}"
            )
            assert "o" not in edge_targets, (
                f"migration did not remove output node edge; "
                f"remaining targets: {edge_targets}"
            )
            node_ids = {n.get("id") for n in (row.nodes or [])}
            assert "i" not in node_ids, "migration kept legacy input node"
            assert "o" not in node_ids, "migration kept legacy output node"
    finally:
        session_module.session_scope = original

# ─────────────────────────────────────────────────────────────────
# Orphan cleanup: a built-in template row whose JSON
# source has been removed from the folder must be deleted on the
# next seed, not left behind as an accumulating ghost. Regression
# net for the bug where the gallery showed 14 templates after the
# 13→5 cleanup because the seed's previous "leave orphans alone"
# policy kept the stale rows in the DB.
# ─────────────────────────────────────────────────────────────────
def test_seed_deletes_orphan_template_rows(engine):
    """Seed once to populate the DB with the 5 current built-ins, then
    hand-insert a fake `tpl-orphan` row + a fake `tpl-orphan-zh-CN`
    row (both `is_template=True`, neither matching any JSON file),
    then re-run the seed and assert both are gone while the 5
    current built-ins survive."""
    from app.templates_loader import discover_templates

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
        # First seed — populate the DB with the 5 current built-ins.
        _seed_templates()

        expected_count = len(discover_templates())
        with SessionLocal() as s:
            assert s.query(Workflow).filter_by(is_template=True).count() == expected_count

        # Inject two orphans.
        with SessionLocal() as s:
            for orphan_id in ("tpl-orphan", "tpl-orphan-zh-CN"):
                s.add(Workflow(
                    id=orphan_id,
                    name="Ghost Template",
                    description="Row whose JSON file no longer exists.",
                    nodes=[],
                    edges=[],
                    is_template=True,
                    category="starter",
                    locale="en" if orphan_id == "tpl-orphan" else "zh-CN",
                ))
            s.commit()

        # Sanity: 2 orphans added on top.
        with SessionLocal() as s:
            assert s.query(Workflow).filter_by(is_template=True).count() == expected_count + 2

        # Re-seed — orphans must be deleted, the 5 built-ins survive.
        _seed_templates()

        with SessionLocal() as s:
            surviving_ids = {
                row.id for row in s.query(Workflow).filter_by(is_template=True).all()
            }
        assert surviving_ids == {entry["id"] for entry in discover_templates()}, (
            f"expected only the {expected_count} JSON-sourced rows to survive, "
            f"got {surviving_ids}"
        )
        assert "tpl-orphan" not in surviving_ids, "orphan row was not deleted"
        assert "tpl-orphan-zh-CN" not in surviving_ids, "orphan zh-CN row was not deleted"
    finally:
        session_module.session_scope = original

# ─────────────────────────────────────────────────────────────────
# Locale — each template carries the language tag
# declared in its JSON `locale` field. The loader, seed, schema
# projection, and `Workflow.locale` column must all agree so the
# frontend can filter the gallery without re-parsing the JSON files.
# ─────────────────────────────────────────────────────────────────
def test_loader_stamps_locale_on_every_entry():
    """Every discovered template must have a `locale` field. The
    default is `"en"` for legacy files (none in this repo, but the
    loader still has to honour the rule for robustness)."""
    from app.templates_loader import discover_templates
    for entry in discover_templates():
        assert entry.get("locale") in {"en", "zh-CN"}, (
            f"{entry['id']}: locale must be 'en' or 'zh-CN', "
            f"got {entry.get('locale')!r}"
        )

def test_loader_yields_both_english_and_chinese_versions():
    """The folder carries one `tpl-<slug>.json` per language, so
    `discover_templates()` must return pairs (one `en` and one
    `zh-CN` per slug) — and exactly one of each per slug."""
    from app.templates_loader import discover_templates
    by_slug: dict[str, set[str]] = {}
    for entry in discover_templates():
        # `tpl-<slug>-zh-CN` and `tpl-<slug>` — drop the suffix to key.
        key = entry["id"]
        if key.endswith("-zh-CN"):
            key = key[: -len("-zh-CN")]
        by_slug.setdefault(key, set()).add(entry["locale"])
    # Every slug must have both locales.
    for slug, locales in by_slug.items():
        assert locales == {"en", "zh-CN"}, (
            f"{slug}: expected both 'en' and 'zh-CN', got {locales}"
        )

def test_seed_persists_locale_on_each_template_row(engine, seeded):
    """The DB column `Workflow.locale` mirrors the JSON's `locale`
    field, so the API can filter by language without re-reading
    the JSON files."""
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as s:
        rows = s.query(Workflow).filter_by(is_template=True).all()
    # Both English and Chinese rows are present.
    locales = {row.locale for row in rows}
    assert locales == {"en", "zh-CN"}, (
        f"expected both 'en' and 'zh-CN' rows, got {locales}"
    )
    # Every row has a non-empty locale (the default is "en").
    assert all((row.locale or "").strip() for row in rows)

def test_list_templates_exposes_locale_field(client, seeded):
    """The gallery response must include `locale` so the frontend can
    filter by language without parsing the id suffix."""
    r = client.get("/api/v1/workflows/templates")
    assert r.status_code == 200
    body = r.json()
    assert body, "expected at least one template row"
    for row in body:
        assert "locale" in row, (
            f"{row.get('id')}: response missing 'locale'"
        )
        assert row["locale"] in {"en", "zh-CN"}
    # Spot-check that BOTH languages appear in the gallery payload.
    locales = {row["locale"] for row in body}
    assert locales == {"en", "zh-CN"}

def test_get_template_exposes_locale_field(client, seeded):
    r = client.get("/api/v1/workflows/templates/tpl-hello-world")
    assert r.status_code == 200
    body = r.json()
    assert body["locale"] == "en"
    r2 = client.get(
        "/api/v1/workflows/templates/tpl-hello-world-zh-CN"
    )
    assert r2.status_code == 200
    assert r2.json()["locale"] == "zh-CN"

def test_migrate_backfills_empty_locale(engine, seeded):
    """A legacy row whose `locale` ended up NULL or empty must be
    backfilled to `"en"` so the API never returns an empty value."""
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
        # Manually clear locale on a row that exists.
        with SessionLocal() as s:
            row = s.query(Workflow).filter_by(id="tpl-hello-world").one()
            row.locale = ""
            s.commit()

        # Re-run the seed/migration path.
        _seed_templates()

        with SessionLocal() as s:
            row = s.query(Workflow).filter_by(id="tpl-hello-world").one()
            assert row.locale == "en", (
                f"migration did not backfill empty locale; "
                f"got {row.locale!r}"
            )
    finally:
        session_module.session_scope = original

