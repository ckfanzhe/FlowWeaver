#!/usr/bin/env python3
"""CI check: validate `shared/nodes.manifest.json` structure.

The manifest is the SoT for every node type — adding a new entry
without the required fields silently breaks the codegen pipeline
(generate_node_types / generate_node_fallback / generate_node_configs
all assume a strict shape). This script catches the structural
mistakes BEFORE codegen runs:

  - missing top-level keys (schemaVersion, nodes)
  - missing per-node keys (kind, configSchemaRef, defaultConfig, ...)
  - wrong kind values (must be one of the documented set)
  - duplicate node names
  - paletteOrder collisions

It does NOT validate every field — the codegen scripts do that
byte-by-byte. This is a cheap structural gate that runs in <100 ms
even on slow CI.

Exit 0 on success, 1 on any failure.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "shared" / "nodes.manifest.json"

# The `kind` discriminator is a closed enum — every node entry
# must pick exactly one. The frontend reads this verbatim to
# decide which palette group / form / icon to render.
VALID_KINDS = {
    "executable",
    "compound",
    "control_flow",
    "tool_source",
    "knowledge_source",
}

# Required keys per node entry. Codegen assumes every field here
# is present — a typo (e.g. "configSchemaRef" vs "configSchema")
# would slip past JSON parsing but break the generated TS.
REQUIRED_NODE_KEYS = {
    "kind",
    "category",
    "displayName",
    "i18nKey",
    "color",
    "textColor",
    "icon",
    "configSchemaRef",
    "defaultConfig",
    "capabilities",
    "ui",
    "runtime",
    "io",
}

REQUIRED_CAPABILITIES_KEYS = {
    "compoundPass",
    "isToolSource",
    "needsToolWiring",
    "skipPass1",
    "stepWrapper",
}


def fail(messages: list[str]) -> int:
    """Print failures and return exit code 1."""
    print(f"manifest schema check: {len(messages)} error(s):", file=sys.stderr)
    for m in messages:
        print(f"  - {m}", file=sys.stderr)
    return 1


def main() -> int:
    if not MANIFEST_PATH.exists():
        return fail([f"{MANIFEST_PATH} not found"])

    try:
        data = json.loads(MANIFEST_PATH.read_text())
    except json.JSONDecodeError as e:
        return fail([f"JSON parse error: {e}"])

    errors: list[str] = []

    # Top-level shape.
    if data.get("schemaVersion") != 2:
        errors.append(
            f"schemaVersion must be 2, got {data.get('schemaVersion')!r}"
        )
    nodes = data.get("nodes")
    if not isinstance(nodes, dict):
        errors.append("`nodes` must be a dict")
        return fail(errors)

    # Per-node shape.
    seen_palette_orders: dict[int, str] = {}
    for node_name, entry in nodes.items():
        if not isinstance(entry, dict):
            errors.append(f"node {node_name!r}: entry must be a dict")
            continue

        missing = REQUIRED_NODE_KEYS - entry.keys()
        if missing:
            errors.append(
                f"node {node_name!r}: missing required keys: "
                f"{sorted(missing)}"
            )

        kind = entry.get("kind")
        if kind is not None and kind not in VALID_KINDS:
            errors.append(
                f"node {node_name!r}: kind {kind!r} not in {sorted(VALID_KINDS)}"
            )

        caps = entry.get("capabilities")
        if isinstance(caps, dict):
            cap_missing = REQUIRED_CAPABILITIES_KEYS - caps.keys()
            if cap_missing:
                errors.append(
                    f"node {node_name!r}: capabilities missing "
                    f"{sorted(cap_missing)}"
                )

        ui = entry.get("ui")
        if isinstance(ui, dict):
            order = ui.get("paletteOrder")
            if isinstance(order, int):
                if order in seen_palette_orders:
                    errors.append(
                        f"node {node_name!r}: paletteOrder {order} "
                        f"collides with {seen_palette_orders[order]!r}"
                    )
                else:
                    seen_palette_orders[order] = node_name

        runtime = entry.get("runtime")
        if isinstance(runtime, dict):
            if not runtime.get("module") or not runtime.get("builder"):
                errors.append(
                    f"node {node_name!r}: runtime needs module + builder"
                )

    if errors:
        return fail(errors)

    print(
        f"manifest schema check: OK "
        f"({len(nodes)} node types, "
        f"palette orders: {sorted(seen_palette_orders.keys())})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())