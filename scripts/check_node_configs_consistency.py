#!/usr/bin/env python3
"""CI check: generated node-config TS interfaces stay consistent with
the Pydantic schemas.

The per-node TypeScript config interfaces used to be
hand-mirrored in `workflow.ts:99-330` and drifted from
`backend/src/app/schemas/node_configs.py` on every Pydantic edit.
This script re-runs `generate_node_configs_ts.generate()` in-memory
and compares the output byte-for-byte against
`frontend/src/types/node-configs.generated.ts`.

Exit code 0 on success, 1 on any failure. Two failure modes the
check catches:

  1. Someone added a field to `node_configs.py` and forgot to run
     `python scripts/generate_node_configs_ts.py` — the generated
     file is stale.
  2. Someone hand-edited `node-configs.generated.ts` — the file
     diverged from the Pydantic schemas.

Mirrors the pattern in `check_node_types_consistency.py`.
"""
from __future__ import annotations

import difflib
import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATED_PATH = REPO_ROOT / "frontend" / "src" / "types" / "node-configs.generated.ts"


def _load_generate() -> callable:
    """Import the generator module from the scripts directory.

    Adding the scripts dir to `sys.path` lets us import the module
    directly without packaging it. The generator module itself
    adjusts its own sys.path to reach the backend — see
    `generate_node_configs_ts.py`'s header.
    """
    scripts_dir = REPO_ROOT / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    if str(REPO_ROOT / "backend" / "src") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))
    mod = importlib.import_module("generate_node_configs_ts")
    return mod.generate


def main() -> int:
    failures: list[str] = []

    if not GENERATED_PATH.exists():
        print(
            f"FAIL: generated file missing at {GENERATED_PATH.relative_to(REPO_ROOT)} — "
            "run `python scripts/generate_node_configs_ts.py`",
            file=sys.stderr,
        )
        return 1

    try:
        generate = _load_generate()
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: cannot import generator: {e}", file=sys.stderr)
        return 1

    expected = generate()
    actual = GENERATED_PATH.read_text(encoding="utf-8")

    if expected == actual:
        print(
            f"OK: {GENERATED_PATH.relative_to(REPO_ROOT)} matches "
            f"backend/src/app/schemas/node_configs.py"
        )
        return 0

    failures.append(
        f"{GENERATED_PATH.relative_to(REPO_ROOT)} drifted from "
        f"backend/src/app/schemas/node_configs.py — re-run "
        f"`python scripts/generate_node_configs_ts.py`"
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