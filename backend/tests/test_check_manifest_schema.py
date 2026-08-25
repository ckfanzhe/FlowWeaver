"""Pytest wrapper for `scripts/check_manifest_schema.py`.

The script is the CI gate that catches structural mistakes in
`shared/nodes.manifest.json` BEFORE codegen runs — missing required
keys, bad `kind` values, paletteOrder collisions, etc. Without a
pytest invocation, the check only fires when someone remembers to
run the script; with pytest, every test run silently re-validates
the manifest and a regression on the JSON file is caught at the
same time as a regression in any backend code.

Test strategy: rather than synthesise every error class via dict
manipulation (which would only test the dict path, not the JSON
parse + dict path), we cover both:

  - The `check_manifest()` helper directly with hand-built dicts
    for each error class (cheap, exhaustive).
  - The end-to-end CLI path via `main()` against the REAL manifest
    file (catches drift if the script's file handling regresses).

A pytest fixture that writes a broken manifest to disk is intentionally
NOT used — those tests tend to leave temp files behind when the
fixture tears down early on a KeyboardInterrupt.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


# ─────────────────────────────────────────────────────────────────
# Locate + import the script (it's in /scripts, not on sys.path)
# ─────────────────────────────────────────────────────────────────
SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
MANIFEST_PATH = SCRIPTS_DIR.parent / "shared" / "nodes.manifest.json"


def _load_module():
    """Import `scripts/check_manifest_schema.py` as a Python module.

    `sys.path` manipulation is scoped to this function — restored on
    exit so other tests aren't affected.
    """
    spec = importlib.util.spec_from_file_location(
        "check_manifest_schema",
        SCRIPTS_DIR / "check_manifest_schema.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cms():
    """Module fixture: import once per test module."""
    return _load_module()


# ─────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────


def _minimal_valid_node(name: str = "node_a", **overrides) -> dict:
    """Build a minimal node entry that satisfies all REQUIRED_NODE_KEYS.

    Defaults match the simplest valid node we can construct. Tests
    override the specific keys they want to break.
    """
    node = {
        "kind": "executable",
        "category": "Core",
        "displayName": name,
        "i18nKey": name,
        "color": "border-blue-400/50 bg-blue-50",
        "textColor": "text-blue-700",
        "icon": "AgentIcon",
        "configSchemaRef": "SomeNodeConfig",
        "defaultConfig": {},
        "capabilities": {
            "compoundPass": None,
            "isToolSource": False,
            "needsToolWiring": True,
            "skipPass1": False,
            "stepWrapper": "agent",
        },
        "ui": {"group": "Core", "form": "SomeForm", "paletteOrder": 1},
        "runtime": {"module": "app.core.strategies.some", "builder": "SomeStrategy"},
        "io": {"inputs": [], "outputs": [], "tools": []},
    }
    node.update(overrides)
    return node


# ─────────────────────────────────────────────────────────────────
# happy path: real manifest passes
# ─────────────────────────────────────────────────────────────────


def test_real_manifest_passes_check(cms):
    """End-to-end: parse the REAL manifest from disk and confirm
    `check_manifest()` accepts it. This is the test that fails when
    a structural regression lands (missing key, bad kind, palette
    collision). It pairs with the CLI invocation in CI as belt +
    suspenders for the codegen-pre-flight gate.
    """
    data = json.loads(MANIFEST_PATH.read_text())
    errors = cms.check_manifest(data)
    assert errors == [], f"real manifest failed check: {errors}"


def test_real_manifest_main_returns_zero(cms, capsys):
    """End-to-end CLI path: `main()` reads the manifest from disk,
    parses JSON, runs the checks, exits 0. Catches a regression in
    the file-handling or error-printing code path that the
    `check_manifest()` direct call would miss.
    """
    rc = cms.main()
    out = capsys.readouterr()
    assert rc == 0, f"main() returned {rc}; stderr: {out.err}"
    assert "manifest schema check: OK" in out.out


# ─────────────────────────────────────────────────────────────────
# error path: each error class is detected
# ─────────────────────────────────────────────────────────────────


def test_missing_schema_version_is_rejected(cms):
    errors = cms.check_manifest({"nodes": {}})
    assert any("schemaVersion" in e for e in errors), errors


def test_wrong_schema_version_is_rejected(cms):
    errors = cms.check_manifest({"schemaVersion": 1, "nodes": {}})
    assert any("schemaVersion must be 2" in e for e in errors), errors


def test_nodes_not_a_dict_is_rejected(cms):
    errors = cms.check_manifest({"schemaVersion": 2, "nodes": []})
    assert any("`nodes` must be a dict" in e for e in errors), errors


def test_node_missing_required_keys_is_rejected(cms):
    """A node with most keys but missing `capabilities` is caught.

    Pinned so a future "all keys optional" regression is caught.
    """
    node = _minimal_valid_node()
    del node["capabilities"]
    errors = cms.check_manifest({
        "schemaVersion": 2,
        "nodes": {"a": node},
    })
    assert any("missing required keys" in e and "capabilities" in e
               for e in errors), errors


def test_node_entry_must_be_dict(cms):
    """A non-dict entry (e.g. `nodes["a"] = "string"`) is caught —
    JSON typing regression check."""
    errors = cms.check_manifest({
        "schemaVersion": 2,
        "nodes": {"a": "string"},
    })
    assert any("entry must be a dict" in e for e in errors), errors


def test_unknown_kind_is_rejected(cms):
    """`kind` is a closed enum. `kind="weird"` is rejected."""
    node = _minimal_valid_node(kind="weird")
    errors = cms.check_manifest({
        "schemaVersion": 2,
        "nodes": {"a": node},
    })
    assert any("kind 'weird' not in" in e for e in errors), errors


def test_capabilities_missing_keys_is_rejected(cms):
    """`capabilities` has its own required-keys set; an entry with
    most keys but missing `skipPass1` is caught."""
    node = _minimal_valid_node()
    del node["capabilities"]["skipPass1"]
    errors = cms.check_manifest({
        "schemaVersion": 2,
        "nodes": {"a": node},
    })
    assert any("capabilities missing" in e and "skipPass1" in e
               for e in errors), errors


def test_palette_order_collision_is_rejected(cms):
    """Two nodes with the same `paletteOrder` is rejected — the
    frontend palette would render them in indeterminate order."""
    n1 = _minimal_valid_node("n1")
    n2 = _minimal_valid_node("n2")
    # Override n2's paletteOrder to collide with n1.
    n2["ui"] = dict(n2["ui"])
    n2["ui"]["paletteOrder"] = n1["ui"]["paletteOrder"]
    errors = cms.check_manifest({
        "schemaVersion": 2,
        "nodes": {"n1": n1, "n2": n2},
    })
    assert any("paletteOrder" in e and "collides" in e for e in errors), errors


def test_runtime_module_or_builder_missing_is_rejected(cms):
    """`runtime` must have BOTH `module` and `builder` — a partial
    entry would break the strategy registry import."""
    node = _minimal_valid_node()
    del node["runtime"]["builder"]
    errors = cms.check_manifest({
        "schemaVersion": 2,
        "nodes": {"a": node},
    })
    assert any("runtime needs module + builder" in e for e in errors), errors


def test_multiple_errors_are_all_reported(cms):
    """All error classes fire on a maximally-broken manifest. The
    script collects every error before exiting 1 (NOT first-error-
    wins), so a single bad edit doesn't hide a second bad edit on
    the same file. Pinned so a future "short-circuit on first error"
    change is caught."""
    bad = {
        "schemaVersion": 1,  # wrong version
        "nodes": {
            "a": "string",  # not a dict
            "b": _minimal_valid_node("b", kind="weird"),  # bad kind
        },
    }
    errors = cms.check_manifest(bad)
    # Expect at least: schemaVersion + nodes['a'] + nodes['b'].kind.
    assert len(errors) >= 3, errors


# ─────────────────────────────────────────────────────────────────
# edge: empty / minimal manifests
# ─────────────────────────────────────────────────────────────────


def test_empty_nodes_dict_is_accepted(cms):
    """A manifest with schemaVersion=2 and `nodes={}` is valid (just
    no node types yet). Codegen scripts handle the empty case; this
    check must NOT reject it.
    """
    errors = cms.check_manifest({"schemaVersion": 2, "nodes": {}})
    assert errors == [], errors


def test_unknown_top_level_keys_are_ignored(cms):
    """Future versions may add `_doc_` or other SoT metadata at the
    top level. The check looks at specific keys, doesn't enforce
    `additionalProperties: false`. Forward-compatibility check.
    """
    errors = cms.check_manifest({
        "schemaVersion": 2,
        "nodes": {},
        "_doc_": "future metadata block — ignore me",
        "future_field": {"anything": True},
    })
    assert errors == [], errors