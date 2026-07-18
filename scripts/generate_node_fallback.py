#!/usr/bin/env python3
"""Generate `frontend/src/components/Nodes/nodeFallback.generated.ts`
from the shared manifest.

The frontend's first-paint fallback used to be a hand-maintained
`FALLBACK_MANIFEST` block in
`frontend/src/components/Nodes/nodeStyles.ts` that duplicated the
backend's manifest — and drifted from it (`parallel.skipPass1` was
false in the fallback but true in the manifest, `group` flipped
between "Core" / "Flow" / "Tools" / "Connectors", etc.). The drift
was silent because nothing read those fields from the fallback.

This script replaces the hand-maintained copy with a derived copy
that always matches the manifest. The transformations it applies:

  * `category` (legacy field, 'executable' | 'tool_source') is
    derived from `kind` (kind='tool_source' → 'tool_source'; kind=
    'executable' | 'compound' → 'executable'). The backend's manifest
    stores a palette-group label under the same key — those values
    ("Core", "Data", …) would fail TS if we passed them through.
  * `paletteOrder` is flattened from `ui.paletteOrder` (presets
    inherit the field position from the UI block).
  * `defaultConfig` is flattened from `overrides.defaultConfig`
    (presets) or the top-level `defaultConfig` (base types).
  * `extends` is nulled for base types, left as-is for presets.

Output is byte-stable (sorted keys, deterministic field order) so
the companion CI check can detect drift.

Run:
    python scripts/generate_node_fallback.py

Companion CI check:
    python scripts/check_node_fallback_consistency.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "shared" / "nodes.manifest.json"
OUT_PATH = REPO_ROOT / "frontend" / "src" / "components" / "Nodes" / "nodeFallback.generated.ts"


def _load_manifest() -> dict:
    with MANIFEST_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _flatten_entry(name: str, entry: dict) -> dict:
    """Map a manifest entry to the `NodeTypeManifestEntry` shape the
    frontend's `NodeTypesManifest` type expects.

    See module docstring for the transformation rationale.
    """
    kind = entry.get("kind") or "executable"
    legacy_category = "tool_source" if kind == "tool_source" else "executable"

    # paletteOrder lives under `ui`; presets override via the same
    # path. `paletteOrder` may be missing on a malformed entry — fall
    # back to a high value so it sorts last in the palette.
    palette_order = (
        (entry.get("ui") or {}).get("paletteOrder")
        if (entry.get("ui") or {}).get("paletteOrder") is not None
        else 999
    )

    # defaultConfig: presets nest under `overrides.defaultConfig`,
    # base types put it at the top level.
    default_config = (
        (entry.get("overrides") or {}).get("defaultConfig")
        if "overrides" in entry
        else entry.get("defaultConfig") or {}
    )

    capabilities = entry.get("capabilities") or {
        "compoundPass": None,
        "isToolSource": kind == "tool_source",
        "needsToolWiring": False,
        "skipPass1": False,
        "stepWrapper": "none",
    }
    ui = entry.get("ui") or {"group": "Other", "form": "HttpForm", "paletteOrder": palette_order}
    io = entry.get("io") or {"inputs": [], "outputs": [], "tools": []}

    return {
        "category": legacy_category,
        "kind": kind,
        "extends": entry.get("extends"),
        "displayName": entry.get("displayName") or name,
        "i18nKey": entry.get("i18nKey") or name,
        "color": entry.get("color") or "",
        "textColor": entry.get("textColor") or "",
        "icon": entry.get("icon") or "",
        "paletteOrder": palette_order,
        "ui": ui,
        "capabilities": capabilities,
        "defaultConfig": default_config,
        "io": io,
    }


def generate(manifest: dict) -> str:
    nodes = manifest.get("nodes") or {}
    if not nodes:
        raise SystemExit("manifest has no nodes block — refusing to generate")

    # Sort entries by name for byte-stability. The output order
    # doesn't affect runtime — the consumer reads by key — but a
    # sorted order keeps diffs small and the CI check happy.
    names = sorted(nodes.keys())
    entries = {n: _flatten_entry(n, nodes[n]) for n in names}

    # Compact JSON. TS will validate the shape via the cast on the
    # import site — `as NodeTypesManifest`.
    body_json = json.dumps(
        {
            "schemaVersion": manifest.get("schemaVersion", 2),
            "types": names,
            "entries": entries,
        },
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )

    lines = [
        "/**",
        " * GENERATED FILE — DO NOT EDIT.",
        " *",
        " * Source of truth: shared/nodes.manifest.json.",
        " * Regenerate with:  python scripts/generate_node_fallback.py",
        " * CI check:         python scripts/check_node_fallback_consistency.py",
        " *",
        " * First-paint fallback derived from the manifest instead",
        " * of hand-maintained. Replaces the inline FALLBACK_MANIFEST",
        " * block in `nodeStyles.ts` so the two can't drift (the",
        " * parallel.skipPass1 mismatch was the canonical example).",
        " */",
        "import type { NodeTypesManifest } from '../../api/nodeTypes'",
        "",
        "// Hand-rolled JSON.parse instead of a static `import` so we",
        "// can round-trip the generated body verbatim — keeps the CI",
        "// diff small when entries change.",
        f"const _BODY = {body_json!r};",
        "",
        "export const NODE_FALLBACK_MANIFEST: NodeTypesManifest = JSON.parse(_BODY) as NodeTypesManifest;",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    if not MANIFEST_PATH.exists():
        print(f"FAIL: manifest missing at {MANIFEST_PATH}", file=sys.stderr)
        return 1
    try:
        manifest = _load_manifest()
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: cannot parse {MANIFEST_PATH}: {e}", file=sys.stderr)
        return 1

    body = generate(manifest)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(OUT_PATH.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(OUT_PATH)

    print(
        f"wrote {OUT_PATH.relative_to(REPO_ROOT)} "
        f"({len(manifest.get('nodes', {}))} node types)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())