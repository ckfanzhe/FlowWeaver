"""Manifest + node-type-registry regression tests.

These tests pin the v1→v2 normaliser, the v2 Pydantic schema, and the
preset-inheritance resolver. Existing emitter / tool-wiring /
IR tests cover the runtime behavior; this file is the manifest's
contract.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core import node_types as nt

# ─────────────────────────────────────────────────────────────────
# Fixtures — synthetic v1 + v2 manifests so we can exercise both
# parse paths without touching the live shared/nodes.manifest.json.
# ─────────────────────────────────────────────────────────────────
V1_MINIMAL = {
    "schemaVersion": 1,
    "nodes": {
        "agent": {
            "category": "executable",
            "displayName": "Agent",
            "i18nKey": "agent",
            "color": "c1", "textColor": "tc1", "icon": "AgentIcon",
            "paletteOrder": 1,
            "configSchemaRef": "AgentNodeConfig",
            "defaultConfig": {"instructions": ""},
            "emitter": {"module": "x", "needsToolWiring": True},
            "runtime": {"module": "app.core.strategies.agent", "builder": "AgentStrategy"},
            "io": {"inputs": ["text"], "outputs": ["text"], "tools": []},
        },
        # The three tool-source types (http / mcp / tools) collapsed
        # into a single `tool` node with `source` discriminator.
        # This fixture simulates the post-merge shape — the merged
        # `tool` entry carries an HTTP-flavoured defaultConfig.
        "tool": {
            "category": "tool_source",
            "displayName": "Tool",
            "i18nKey": "tool",
            "color": "c2", "textColor": "tc2", "icon": "ToolIcon",
            "paletteOrder": 2,
            "paletteGroup": "Data",
            "configSchemaRef": "ToolNodeConfig",
            "defaultConfig": {"source": "http", "toolName": "http_call", "baseUrl": ""},
            "emitter": {"module": "x"},
            "runtime": {"module": "app.core.strategies.tool", "builder": "ToolStrategy"},
            "io": {"inputs": [], "outputs": ["tool_call"], "tools": ["http_call"]},
        },
    },
}

V2_MINIMAL = {
    "schemaVersion": 2,
    "nodes": {
        "agent": {
            "kind": "executable",
            "category": "Core",
            "displayName": "Agent",
            "i18nKey": "agent",
            "color": "c1", "textColor": "tc1", "icon": "AgentIcon",
            "configSchemaRef": "AgentNodeConfig",
            "defaultConfig": {"instructions": ""},
            "capabilities": {
                "compoundPass": None, "isToolSource": False,
                "needsToolWiring": True, "skipPass1": False,
                "stepWrapper": "agent",
            },
            "ui": {"group": "Core", "form": "AgentForm", "paletteOrder": 1},
            "runtime": {"module": "app.core.strategies.agent", "builder": "AgentStrategy"},
            "io": {"inputs": ["text"], "outputs": ["text"], "tools": []},
        },
        "tool": {
            "kind": "tool_source",
            "category": "Data",
            "displayName": "Tool",
            "i18nKey": "tool",
            "color": "c2", "textColor": "tc2", "icon": "ToolIcon",
            "configSchemaRef": "ToolNodeConfig",
            "defaultConfig": {"source": "http", "toolName": "http_call", "baseUrl": ""},
            "capabilities": {
                "compoundPass": None, "isToolSource": True,
                "needsToolWiring": False, "skipPass1": False,
                "stepWrapper": "none",
            },
            "ui": {"group": "Data", "form": "ToolForm", "paletteOrder": 5},
            "runtime": {"module": "app.core.strategies.tool", "builder": "ToolStrategy"},
            "io": {"inputs": [], "outputs": ["tool_call"], "tools": ["http_call"]},
        },
    },
}

V2_MINIMAL_PRESET = {
    "schemaVersion": 2,
    "nodes": {
        **V2_MINIMAL["nodes"],
        "wikipedia": {
            "kind": "tool_source",
            "extends": "tool",
            "displayName": "Wikipedia",
            "ui": {"group": "Search", "form": "ToolPresetForm", "paletteOrder": 7},
            "overrides": {
                "defaultConfig": {
                    "source": "http",
                    "toolName": "wikipedia_search",
                    "baseUrl": "https://en.wikipedia.org",
                },
            },
            "runtime": {"module": "app.core.strategies.tool", "builder": "ToolStrategy"},
            "io": {"inputs": [], "outputs": ["tool_call"], "tools": ["wikipedia_search"]},
        },
    },
}

@pytest.fixture
def reset_manifest_cache(monkeypatch):
    """Clear all lru_caches that depend on the manifest file so each
    test can swap `_load_manifest_path` to a synthetic value.

    `_load_manifest_path` is `lru_cache`-wrapped at module level but
    tests may have monkeypatched it with a plain lambda — guard the
    clear with `hasattr` so teardown doesn't fail.
    """
    def _safe_clear(fn):
        clear = getattr(fn, "cache_clear", None)
        if callable(clear):
            clear()

    _safe_clear(nt._load_manifest_path)
    _safe_clear(nt._validated_manifest)
    _safe_clear(nt._build_registry)
    _safe_clear(nt._runtime_builders)
    yield
    _safe_clear(nt._load_manifest_path)
    _safe_clear(nt._validated_manifest)
    _safe_clear(nt._build_registry)
    _safe_clear(nt._runtime_builders)

def _patch_manifest(monkeypatch, payload: dict) -> None:
    """Point `_load_manifest_path` at an in-memory JSON blob AND
    rebuild the module-level `NODE_TYPES` / `PALETTE_ORDER` dicts so
    later assertions read the synthetic data.

    Tolerates manifests that fail to parse (e.g. tests asserting
    `pytest.raises(ValueError)` on a bad schemaVersion). On failure
    we restore the previously-cached `NODE_TYPES` / `PALETTE_ORDER`
    via `monkeypatch.setattr` so subsequent tests in the same
    pytest run still see the live registry — otherwise the next
    test that reads `NODE_TYPES` finds an empty dict left over from
    this failure path.
    """
    monkeypatch.setattr(nt, "_load_manifest_path", lambda: payload)
    # Snapshot the live registry so we can restore on either
    # success or failure. `monkeypatch.setattr` registers the
    # restoration; teardown runs it automatically when the
    # enclosing test exits.
    monkeypatch.setattr(nt, "NODE_TYPES", dict(nt.NODE_TYPES))
    monkeypatch.setattr(nt, "PALETTE_ORDER", nt.PALETTE_ORDER)
    try:
        specs = list(nt._build_registry())
    except (ValueError, Exception):
        nt.NODE_TYPES = {}
        nt.PALETTE_ORDER = ()
        return
    nt.NODE_TYPES = {s.name: s for s in specs}
    nt.PALETTE_ORDER = tuple(
        s.name for s in sorted(specs, key=lambda s: s.palette_order)
    )

# ─────────────────────────────────────────────────────────────────
# v1 → v2 normaliser
# ─────────────────────────────────────────────────────────────────
class TestV1Normaliser:
    def test_v1_manifest_normalises_to_v2(self, monkeypatch, reset_manifest_cache):
        _patch_manifest(monkeypatch, V1_MINIMAL)
        manifest = nt._validated_manifest()
        assert manifest.schemaVersion == 2
        assert set(manifest.nodes) == {"agent", "tool"}

    def test_v1_agent_becomes_executable_with_step_wrapper_agent(
        self, monkeypatch, reset_manifest_cache
    ):
        _patch_manifest(monkeypatch, V1_MINIMAL)
        spec = nt.NODE_TYPES["agent"]
        assert spec.kind == "executable"
        assert spec.capabilities.stepWrapper == "agent"
        assert spec.capabilities.needsToolWiring is True
        assert spec.capabilities.compoundPass is None

    def test_v1_tool_becomes_tool_source(self, monkeypatch, reset_manifest_cache):
        _patch_manifest(monkeypatch, V1_MINIMAL)
        # The legacy `http` entry is gone; the merged `tool`
        # entry replaces it with a single tool_source kind node.
        spec = nt.NODE_TYPES["tool"]
        assert spec.kind == "tool_source"
        assert spec.capabilities.isToolSource is True
        assert spec.capabilities.stepWrapper == "none"
        # paletteGroup from v1 surfaces as palette_group on the resolved spec.
        assert spec.palette_group == "Data"
        assert spec.ui.form == ""  # v1 had no ui.form; phase fills it

    def test_v1_defaultConfig_preserved(self, monkeypatch, reset_manifest_cache):
        _patch_manifest(monkeypatch, V1_MINIMAL)
        # The http-flavoured entry is now the merged `tool`
        # node; the defaultConfig carries an explicit
        # `source: 'http'` discriminator.
        assert nt.NODE_TYPES["tool"].default_config == {
            "source": "http", "toolName": "http_call", "baseUrl": "",
        }

# ─────────────────────────────────────────────────────────────────
# v2 native parse
# ─────────────────────────────────────────────────────────────────
class TestV2Native:
    def test_v2_manifest_passes_through(self, monkeypatch, reset_manifest_cache):
        _patch_manifest(monkeypatch, V2_MINIMAL)
        manifest = nt._validated_manifest()
        assert manifest.schemaVersion == 2
        assert set(manifest.nodes) == {"agent", "tool"}

    def test_v2_capabilities_surfaced(self, monkeypatch, reset_manifest_cache):
        _patch_manifest(monkeypatch, V2_MINIMAL)
        spec = nt.NODE_TYPES["agent"]
        assert spec.kind == "executable"
        assert spec.capabilities.stepWrapper == "agent"
        assert spec.capabilities.needsToolWiring is True
        assert spec.ui.form == "AgentForm"

# ─────────────────────────────────────────────────────────────────
# Preset inheritance (phase implementation is wired here)
#
# `V2_MINIMAL` carries only the base types (the multi-HTTP
# presets were removed in ). To exercise preset
# behaviour we layer a single `wikipedia` preset on top via
# `V2_MINIMAL_PRESET`.
# ─────────────────────────────────────────────────────────────────
class TestPresetInheritance:
    def test_preset_inherits_parent_kind(self, monkeypatch, reset_manifest_cache):
        _patch_manifest(monkeypatch, V2_MINIMAL_PRESET)
        wiki = nt.NODE_TYPES["wikipedia"]
        assert wiki.kind == "tool_source"  # matches http's kind

    def test_preset_default_config_merges_overrides(self, monkeypatch, reset_manifest_cache):
        _patch_manifest(monkeypatch, V2_MINIMAL_PRESET)
        wiki = nt.NODE_TYPES["wikipedia"]
        # Wikipedia's overrides take precedence over http's defaults.
        assert wiki.default_config["toolName"] == "wikipedia_search"
        assert wiki.default_config["baseUrl"] == "https://en.wikipedia.org"

    def test_preset_shares_parent_runtime(self, monkeypatch, reset_manifest_cache):
        _patch_manifest(monkeypatch, V2_MINIMAL_PRESET)
        wiki = nt.NODE_TYPES["wikipedia"]
        # Presets extend the merged `tool` node and inherit its
        # `ToolStrategy` builder.
        assert wiki.runtime_builder_name == "ToolStrategy"
        assert wiki.config_schema is nt.NODE_TYPES["tool"].config_schema

# ─────────────────────────────────────────────────────────────────
# Live manifest has the wikipedia preset shipped (added )
# ─────────────────────────────────────────────────────────────────
class TestLiveManifestHasNoPresets:
    """The live manifest ships with the 9 base types + one preset
    (`wikipedia`, extends: "tool"). Adding more presets is a
    one-block manifest edit — see docs/node-types.md Recipe 1."""

    def test_live_manifest_contains_only_6_base_types(self):
        """Test name kept for back-compat; the actual count is 7 after
        the RAG knowledge node ([[gleaming-munching-grove]]) joined.
        Renamed intent: the live manifest contains ONLY base types —
        no preset entries (those collapsed into the `tool` node's
        `preset` config discriminator)."""
        from app.core.node_types import NODE_TYPES
        # The 5 preset entries collapsed into a `preset` config
        # discriminator on the single `tool` node. The `knowledge`
        # node (RAG) joined as a 7th base type in
        # [[gleaming-munching-grove]]. The manifest now only declares
        # the 7 base node types; presets live in
        # `app.core.strategies.tool.PRESET_REGISTRY`.
        expected = {
            "agent", "branch", "flow", "loop",
            "ask", "tool", "knowledge",
        }
        assert set(NODE_TYPES) == expected

class TestPresetRegistry:
    """: the 5 presets collapsed into the
    `tool` node's `preset` config discriminator. Per-preset metadata
    (toolkit_class + toolkit_methods + default_config) now lives in
    `app.core.strategies.tool.PRESET_REGISTRY`. These tests pin the
    registry shape so future regressions (typo'd class path, missing
    default_config key) surface at unit-test time."""

    def test_registry_has_5_presets(self):
        from app.core.strategies.tool import PRESET_REGISTRY
        assert set(PRESET_REGISTRY) == {
            "wikipedia", "tavily_search", "duckduckgo",
            "calculator", "arxiv_search",
        }

    def test_wikipedia_is_http_preset_with_defaults(self):
        from app.core.strategies.tool import PRESET_REGISTRY
        spec = PRESET_REGISTRY["wikipedia"]
        # HTTP-source preset — falls through to the existing
        # build_http_function path after applying default_config.
        assert spec.toolkit_class is None
        assert spec.default_source == "http"
        # Defaults carried from the old `wikipedia` manifest entry.
        assert spec.default_config["toolName"] == "wikipedia_search"
        assert spec.default_config["baseUrl"] == "https://en.wikipedia.org"
        assert "{query}" in spec.default_config["path"]
        assert spec.default_config["method"] == "GET"

    def test_toolkit_presets_carry_class_and_methods(self):
        from app.core.strategies.tool import PRESET_REGISTRY
        for name, expected_class in [
            ("tavily_search", "agno.tools.tavily.TavilyTools"),
            ("duckduckgo", "agno.tools.duckduckgo.DuckDuckGoTools"),
            ("calculator", "agno.tools.calculator.CalculatorTools"),
            ("arxiv_search", "agno.tools.arxiv.ArxivTools"),
        ]:
            spec = PRESET_REGISTRY[name]
            assert spec.toolkit_class == expected_class
            assert len(spec.toolkit_methods) >= 1
            # Toolkit presets don't override `source` — the toolkit
            # is its own emit primitive.
            assert spec.default_source is None
            assert spec.default_config == {}

# ─────────────────────────────────────────────────────────────────
# Preset edge cases (cycle / unknown parent / kind mismatch)
# ─────────────────────────────────────────────────────────────────
class TestPresetEdgeCases:
    def test_preset_extends_unknown_parent_fails(self, monkeypatch, reset_manifest_cache):
        bad = {
            "schemaVersion": 2,
            "nodes": {
                "wikipedia": {
                    "kind": "tool_source",
                    "extends": "nonexistent",
                    "displayName": "Wikipedia",
                    "configSchemaRef": "ToolNodeConfig",
                    "runtime": {"module": "app.core.strategies.tool", "builder": "ToolStrategy"},
                    "io": {},
                },
            },
        }
        _patch_manifest(monkeypatch, bad)
        with pytest.raises(ValueError, match="extends unknown type"):
            nt._build_registry()

    def test_preset_cycle_fails(self, monkeypatch, reset_manifest_cache):
        # a → b → a (we synthesise this without an actual http entry).
        bad = {
            "schemaVersion": 2,
            "nodes": {
                "a": {
                    "kind": "executable",
                    "extends": "b",
                    "displayName": "A",
                    "configSchemaRef": "AgentNodeConfig",
                    "runtime": {"module": "app.core.strategies.tool", "builder": "ToolStrategy"},
                    "io": {},
                },
                "b": {
                    "kind": "executable",
                    "extends": "a",
                    "displayName": "B",
                    "configSchemaRef": "AgentNodeConfig",
                    "runtime": {"module": "app.core.strategies.tool", "builder": "ToolStrategy"},
                    "io": {},
                },
            },
        }
        _patch_manifest(monkeypatch, bad)
        with pytest.raises(ValueError, match="cycle"):
            nt._build_registry()

    def test_preset_kind_mismatch_fails(self, monkeypatch, reset_manifest_cache):
        # Preset declares `executable` while its parent is `tool_source`.
        bad = {
            "schemaVersion": 2,
            "nodes": {
                "tool": {
                    "kind": "tool_source",
                    "displayName": "Tool",
                    "configSchemaRef": "ToolNodeConfig",
                    "runtime": {"module": "app.core.strategies.tool", "builder": "ToolStrategy"},
                    "io": {},
                },
                "weird": {
                    "kind": "executable",
                    "extends": "tool",
                    "displayName": "Weird",
                    "configSchemaRef": "AgentNodeConfig",
                    "runtime": {"module": "app.core.strategies.tool", "builder": "ToolStrategy"},
                    "io": {},
                },
            },
        }
        _patch_manifest(monkeypatch, bad)
        with pytest.raises(ValueError, match="kind"):
            nt._build_registry()

# ─────────────────────────────────────────────────────────────────
# Schema-version pin
# ─────────────────────────────────────────────────────────────────
class TestSchemaVersionPin:
    def test_unsupported_schema_version_rejected(self, monkeypatch, reset_manifest_cache):
        _patch_manifest(monkeypatch, {"schemaVersion": 99, "nodes": {}})
        with pytest.raises(ValueError, match="unsupported schemaVersion"):
            nt._validated_manifest()

    def test_live_manifest_is_schema_v2(self):
        """The on-disk manifest must stay at v2 — drift here breaks the
        deployed canvas. Updates should go through this test's
        synthetic v2 fixture."""
        manifest_path = Path(__file__).resolve().parents[2] / "shared" / "nodes.manifest.json"
        with manifest_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["schemaVersion"] == 2, (
            f"shared/nodes.manifest.json drifted to schemaVersion "
            f"{data['schemaVersion']}; update fixtures or pin this test"
        )
