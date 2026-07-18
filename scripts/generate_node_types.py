#!/usr/bin/env python3
"""Generate `frontend/src/types/workflow.generated.ts` from the shared
manifest.

Phase 9 (2026-08) of the node-system refactor. The manifest is the
single source of truth for which node types exist; this script
projects that set into a TypeScript literal-union so typecheck can
catch drift between Python and TypeScript.

Output shape (sorted alphabetically, deterministic):

    // ─── DO NOT EDIT — regenerate with scripts/generate_node_types.py
    export type GeneratedNodeType =
      | 'agent'
      | 'brave_search'
      ...
      | 'wikipedia';

Run:
    python scripts/generate_node_types.py

Companion CI check:
    python scripts/check_node_types_consistency.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "shared" / "nodes.manifest.json"
OUT_PATH = REPO_ROOT / "frontend" / "src" / "types" / "workflow.generated.ts"


def _load_manifest() -> dict:
    with MANIFEST_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def generate(manifest: dict) -> str:
    """Return the full file body for `workflow.generated.ts`.

    We sort the node names alphabetically so the output is byte-stable
    — the CI check script compares against this string and any
    nondeterministic order would create noisy diffs.
    """
    nodes = manifest.get("nodes") or {}
    if not nodes:
        raise SystemExit("manifest has no nodes block — refusing to generate")

    names = sorted(nodes.keys())
    lines = [
        "/**",
        " * GENERATED FILE — DO NOT EDIT.",
        " *",
        " * Source of truth: shared/nodes.manifest.json.",
        " * Regenerate with:  python scripts/generate_node_types.py",
        " * CI check:         python scripts/check_node_types_consistency.py",
        " *",
        " * Phase 9 (2026-08) of the node-system refactor. Adding a new",
        " * preset in the manifest (one `extends:` block) automatically",
        " * extends this union — typecheck catches drift between Python",
        " * and TypeScript without anyone having to remember to hand-edit",
        " * two lists.",
        " *",
        " * `workflow.ts` re-exports this type as `NodeType` so existing",
        " * imports (`import type { NodeType } from './workflow'`) keep",
        " * working unchanged.",
        " */",
        "",
        "export type GeneratedNodeType =",
    ]
    for n in names:
        # Literal-union member: `  | 'foo'` (no leading pipe on the first).
        prefix = "  | " if lines[-1] != "export type GeneratedNodeType =" else "    "
        lines.append(f"{prefix}'{n}'")
    lines.append(";")
    lines.append("")  # trailing newline
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

    # Write atomically — write to a temp file then rename, so a crash
    # mid-write doesn't leave a half-written generated.ts on disk.
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(OUT_PATH.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(OUT_PATH)

    print(f"wrote {OUT_PATH.relative_to(REPO_ROOT)} "
          f"({len(manifest.get('nodes', {}))} node types)")
    return 0


if __name__ == "__main__":
    sys.exit(main())