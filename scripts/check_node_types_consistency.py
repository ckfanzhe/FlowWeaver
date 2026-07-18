#!/usr/bin/env python3
"""CI check: generated node-types union stays consistent with manifest.

Phase 9 (2026-08). The script regenerates `workflow.generated.ts`
in memory and compares it byte-for-byte against the file on disk.
Exit 0 on success, 1 on any failure.

Two failure modes the check catches:

  1. Someone added a node to `shared/nodes.manifest.json` and forgot
     to run `python scripts/generate_node_types.py` — the generated
     file is stale.

  2. Someone hand-edited `workflow.generated.ts` (e.g. removed a
     preset to silence a type error) — the file diverged from the
     manifest's set.

We don't auto-write the file from CI; auto-writing makes the
"forgot to commit after generation" failure mode invisible. Surface
it as a CI error so the commit that adds a node type must include
both the manifest entry AND the regenerated union.
"""
from __future__ import annotations

import difflib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "shared" / "nodes.manifest.json"
GENERATED_PATH = REPO_ROOT / "frontend" / "src" / "types" / "workflow.generated.ts"


def _load_manifest() -> dict:
    with MANIFEST_PATH.open("r", encoding="utf-8") as fh:
        import json
        return json.load(fh)


def _expected_body(manifest: dict) -> str:
    # Mirror scripts/generate_node_types.py:generate() so the two
    # stay in lockstep. If you change one, change both.
    nodes = manifest.get("nodes") or {}
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
        prefix = "  | " if lines[-1] != "export type GeneratedNodeType =" else "    "
        lines.append(f"{prefix}'{n}'")
    lines.append(";")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    failures: list[str] = []

    if not MANIFEST_PATH.exists():
        print(f"FAIL: manifest missing at {MANIFEST_PATH}", file=sys.stderr)
        return 1
    if not GENERATED_PATH.exists():
        print(
            f"FAIL: generated file missing at {GENERATED_PATH.relative_to(REPO_ROOT)} — "
            "run `python scripts/generate_node_types.py`",
            file=sys.stderr,
        )
        return 1

    try:
        manifest = _load_manifest()
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: cannot parse {MANIFEST_PATH}: {e}", file=sys.stderr)
        return 1

    expected = _expected_body(manifest)
    actual = GENERATED_PATH.read_text(encoding="utf-8")

    if expected == actual:
        nodes = sorted((manifest.get("nodes") or {}).keys())
        print(
            f"OK: {GENERATED_PATH.relative_to(REPO_ROOT)} matches manifest "
            f"({len(nodes)} node types)"
        )
        return 0

    # Drift detected — print a unified diff so the developer can see
    # exactly what changed without having to re-run the generator.
    failures.append(
        f"{GENERATED_PATH.relative_to(REPO_ROOT)} drifted from "
        f"{MANIFEST_PATH.relative_to(REPO_ROOT)} — re-run "
        f"`python scripts/generate_node_types.py`"
    )
    diff = difflib.unified_diff(
        actual.splitlines(keepends=True),
        expected.splitlines(keepends=True),
        fromfile=f"{GENERATED_PATH.relative_to(REPO_ROOT)} (current)",
        tofile=f"{GENERATED_PATH.relative_to(REPO_ROOT)} (expected)",
        n=3,
    )
    print("\n".join(diff), file=sys.stderr)

    print("\nFAILURES:", file=sys.stderr)
    for f in failures:
        print(f"  - {f}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())