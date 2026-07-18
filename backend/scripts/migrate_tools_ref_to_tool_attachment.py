#!/usr/bin/env python3
"""One-shot migration: cfg.toolsRef → kind=tool_attachment edges.

Phase 1 (2026-08-14): the platform replaced the legacy `cfg.toolsRef`
config field (a list of tool-source node ids on each agent) with
typed `kind="tool_attachment"` edges in the workflow graph. The IR
builder still understands both forms — `tool_attachments` wins over
`tool_refs` when both exist — so pre-migration workflows keep working
without the script. **Run this script to clean them up**.

What it does
------------

For every workflow row in the `workflows` table:

  1. Walk every agent node. Read its `cfg.toolsRef`.
  2. For each tool-source id in that list, ensure an edge with
     `kind="tool_attachment"` exists from that tool node TO the
     agent. Skip edges that already exist (idempotent).
  3. Optionally drop `cfg.toolsRef` from the agent (default: keep
     it — the IR falls back to it if no edge exists, so leaving
     cfg in place is harmless AND gives users a one-line revert
     path). Pass `--drop-cfg-toolsref` to remove the field too.
  4. Persist the updated `nodes` / `edges` JSON columns.

Safety
------

  * Idempotent: running the script twice is a no-op (it never
    creates a duplicate edge and never removes edges that already
    cover a cfg.toolsRef entry).
  * Read-only by default: cfg.toolsRef is preserved so users can
    undo the migration by reverting the JSON row.
  * Logs every change (workflow id, agent id, ref id, action).
  * Validates that the ref points at an actual node in the same
    workflow. Stale cfg entries (refs to missing tool nodes) are
    left alone — they were already silently dropped by the IR.

Usage
-----

    # Default: keep cfg.toolsRef, just add the new typed edges.
    python -m scripts.migrate_tools_ref_to_tool_attachment

    # Aggressive: also drop cfg.toolsRef once the edge is in place.
    python -m scripts.migrate_tools_ref_to_tool_attachment --drop-cfg-toolsref

    # Dry run — print the changes but don't write anything.
    python -m scripts.migrate_tools_ref_to_tool_attachment --dry-run

Exit code 0 on success (including the "nothing to do" case), 1 if
the DB is unreachable or any row's JSON is malformed.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

# Make the project importable when run from anywhere.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app.db.models import Workflow  # noqa: E402
from app.db.session import session_scope  # noqa: E402

log = logging.getLogger("migrate_tools_ref")


def _agent_node_ids(nodes: list[dict]) -> list[str]:
    return [n["id"] for n in nodes if n.get("type") == "agent"]


def _node_map(nodes: list[dict]) -> dict[str, dict]:
    return {n["id"]: n for n in nodes if n.get("id") is not None}


def _edge_exists(edges: list[dict], src: str, tgt: str) -> bool:
    """An edge of kind=tool_attachment from `src` to `tgt` already
    present in the list. Used to avoid duplicates."""
    return any(
        e.get("source") == src
        and e.get("target") == tgt
        and e.get("kind") == "tool_attachment"
        for e in edges
    )


def _add_edge(edges: list[dict], src: str, tgt: str, *, ts_ms: int) -> dict:
    """Append a new tool_attachment edge to `edges` (in place)."""
    eid = f"e-migrate-{src}-{tgt}-{ts_ms}"
    edge = {
        "id": eid,
        "source": src,
        "target": tgt,
        "kind": "tool_attachment",
    }
    edges.append(edge)
    return edge


def migrate_workflow(
    nodes: list[dict],
    edges: list[dict],
    *,
    drop_cfg: bool,
    ts_ms: int,
) -> tuple[list[dict], list[dict], list[str]]:
    """Apply the migration to a single workflow's (nodes, edges) lists.

    Returns `(new_nodes, new_edges, log_lines)`. The lists are mutated
    in place AND returned for clarity (callers that prefer immutable
    semantics can use the returned references).

    `log_lines` lists every change as a one-line string — used by the
    CLI for audit output.
    """
    lines: list[str] = []
    by_id = _node_map(nodes)

    for agent_id in _agent_node_ids(nodes):
        agent_node = by_id[agent_id]
        cfg = (agent_node.get("data") or {}).get("config") or {}
        tools_ref = cfg.get("toolsRef") or []
        if not tools_ref:
            continue

        # Filter out stale refs — pointing at a node that doesn't
        # exist (or isn't a tool-source) — so we don't emit edges
        # that the connection validator would reject.
        valid_refs = [
            ref for ref in tools_ref
            if ref in by_id
            and by_id[ref].get("type") in {"tools", "http", "mcp", "tool"}
        ]
        if not valid_refs:
            lines.append(
                f"agent {agent_id}: no valid toolsRef targets "
                f"(skipped {len(tools_ref)} stale entries)"
            )
            continue

        # Add missing tool_attachment edges (idempotent: skip if
        # an edge already exists for that (src, agent_id) pair).
        added = 0
        for ref in valid_refs:
            if _edge_exists(edges, ref, agent_id):
                continue
            _add_edge(edges, ref, agent_id, ts_ms=ts_ms)
            added += 1

        if added:
            lines.append(
                f"agent {agent_id}: added {added} tool_attachment "
                f"edges (refs={valid_refs!r})"
            )
        else:
            lines.append(
                f"agent {agent_id}: toolsRef already covered by "
                f"{len(valid_refs)} existing edges — no-op"
            )

        if drop_cfg and "toolsRef" in cfg:
            del cfg["toolsRef"]
            lines.append(f"agent {agent_id}: dropped cfg.toolsRef")

    return nodes, edges, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate legacy cfg.toolsRef on agent nodes to typed "
            "kind=tool_attachment edges. Idempotent."
        ),
    )
    parser.add_argument(
        "--drop-cfg-toolsref",
        action="store_true",
        help=(
            "Also remove cfg.toolsRef once the typed edge is in "
            "place. Default keeps it for a one-line revert path."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the changes but don't write to the DB.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print per-workflow change lines (not just the summary).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    ts_ms = int(time.time() * 1000)

    n_workflows = 0
    n_changed = 0
    n_edges_added = 0
    n_cfg_dropped = 0
    n_already_migrated = 0

    try:
        with session_scope() as db:
            workflows = db.query(Workflow).all()
            for wf in workflows:
                n_workflows += 1
                nodes = list(wf.nodes or [])
                edges = list(wf.edges or [])
                if not nodes:
                    continue
                new_nodes, new_edges, lines = migrate_workflow(
                    nodes, edges,
                    drop_cfg=args.drop_cfg_toolsref,
                    ts_ms=ts_ms,
                )

                # Detect whether anything actually changed.
                changed = (
                    new_nodes is not nodes  # never (mutated in place)
                    or new_edges is not edges
                    or any(
                        "dropped cfg.toolsRef" in ln or "added " in ln
                        for ln in lines
                    )
                )

                if not changed:
                    n_already_migrated += 1
                    continue

                # Count per-line outcomes for the summary.
                for ln in lines:
                    if "added " in ln:
                        # Lines like "added 2 tool_attachment edges" — count from.
                        try:
                            n_edges_added += int(ln.split("added ")[1].split(" ")[0])
                        except (IndexError, ValueError):
                            pass
                    if "dropped cfg.toolsRef" in ln:
                        n_cfg_dropped += 1

                if not args.dry_run:
                    wf.nodes = new_nodes
                    wf.edges = new_edges
                n_changed += 1
                if args.verbose:
                    print(f"=== {wf.id} ({wf.name}) ===")
                    for ln in lines:
                        print(f"  - {ln}")
                    if args.dry_run:
                        print("  (dry-run: not written)")
                    print()
    except Exception as e:  # noqa: BLE001
        log.exception("migration failed: %s", e)
        return 1

    # Summary
    print(json.dumps({
        "workflows_seen": n_workflows,
        "workflows_changed": n_changed,
        "workflows_already_migrated": n_already_migrated,
        "edges_added": n_edges_added,
        "cfg_toolsref_dropped": (
            n_cfg_dropped if args.drop_cfg_toolsref else 0
        ),
        "drop_cfg_toolsref": args.drop_cfg_toolsref,
        "dry_run": args.dry_run,
    }, indent=2))

    if args.dry_run:
        print("\nDRY RUN — no changes were written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())