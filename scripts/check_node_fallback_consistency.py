#!/usr/bin/env python3
"""CI check: generated node-fallback file stays consistent with manifest.

The script regenerates
`frontend/src/components/Nodes/nodeFallback.generated.ts` in memory
and compares it byte-for-byte against the file on disk. Exit 0 on
success, 1 on any failure.

Two failure modes the check catches:

  1. Someone added a node type (or preset) to
     `shared/nodes.manifest.json` and forgot to run
     `python scripts/generate_node_fallback.py` — the generated
     file is stale and the frontend's first paint would miss the
     new entry.

  2. Someone hand-edited `nodeFallback.generated.ts` — the file
     diverged from the manifest's set.

We don't auto-write the file from CI; auto-writing makes the
"forgot to commit after generation" failure mode invisible. Surface
it as a CI error so the commit that adds a node type must include
both the manifest entry AND the regenerated fallback.
"""
from __future__ import annotations

import difflib
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "shared" / "nodes.manifest.json"
GENERATED_PATH = REPO_ROOT / "frontend" / "src" / "components" / "Nodes" / "nodeFallback.generated.ts"
GENERATOR_PATH = REPO_ROOT / "scripts" / "generate_node_fallback.py"


def _load_generator_module():
    """Dynamically import the generator script so the check and the
    generator share one definition of the body format."""
    spec = importlib.util.spec_from_file_location(
        "_generate_node_fallback", GENERATOR_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load generator at {GENERATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    failures: list[str] = []

    if not MANIFEST_PATH.exists():
        print(f"FAIL: manifest missing at {MANIFEST_PATH}", file=sys.stderr)
        return 1
    if not GENERATED_PATH.exists():
        print(
            f"FAIL: generated file missing at {GENERATED_PATH.relative_to(REPO_ROOT)} — "
            "run `python scripts/generate_node_fallback.py`",
            file=sys.stderr,
        )
        return 1
    if not GENERATOR_PATH.exists():
        print(f"FAIL: generator missing at {GENERATOR_PATH}", file=sys.stderr)
        return 1

    try:
        generator = _load_generator_module()
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: cannot load generator: {e}", file=sys.stderr)
        return 1

    try:
        manifest = generator._load_manifest()
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: cannot parse {MANIFEST_PATH}: {e}", file=sys.stderr)
        return 1

    expected = generator.generate(manifest)
    actual = GENERATED_PATH.read_text(encoding="utf-8")

    if expected == actual:
        nodes = sorted((manifest.get("nodes") or {}).keys())
        print(
            f"OK: {GENERATED_PATH.relative_to(REPO_ROOT)} matches manifest "
            f"({len(nodes)} node types)"
        )
        return 0

    failures.append(
        f"{GENERATED_PATH.relative_to(REPO_ROOT)} drifted from "
        f"{MANIFEST_PATH.relative_to(REPO_ROOT)} — re-run "
        f"`python scripts/generate_node_fallback.py`"
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