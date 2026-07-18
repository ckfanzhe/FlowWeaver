"""Workflow JSON import/export.

This module is the single source of truth for the on-disk `.json` format
users exchange when sharing workflows. Keep it stable across versions —
the `schemaVersion` field exists so we can migrate older files.

Format (v1):
    {
      "schemaVersion": "1.0",
      "kind": "agnobuilder.workflow",
      "exportedAt": "-12T12:34:56.789Z",  // ISO 8601, set on export
      "workflow": {
        "name": "My flow",
        "description": "...",
        "nodes": [ { id, type, position, data }, ... ],
        "edges": [ { id, source, target, ... }, ... ]
      }
    }

API:
    serialize(workflow: dict) -> dict
    parse(payload: dict) -> dict  (returns the validated `workflow` block)
    WorkflowSchemaError  (raised on bad input)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.core._compat import migrate_envelope
from app.core.graph import GraphError, validate_workflow

SCHEMA_VERSION = "1.0"
KIND = "agnobuilder.workflow"

class WorkflowSchemaError(ValueError):
    """Raised when a JSON payload cannot be turned into a workflow."""

# ─────────────────────────────────────────────────────────────────
# Serialize
# ─────────────────────────────────────────────────────────────────
def serialize(workflow: dict) -> dict:
    """Wrap a workflow dict in the on-disk envelope.

    `workflow` should have keys: name, description?, nodes, edges.
    Extra keys are dropped (the envelope is a strict shape).
    """
    nodes = workflow.get("nodes") or []
    edges = workflow.get("edges") or []
    return {
        "schemaVersion": SCHEMA_VERSION,
        "kind": KIND,
        "exportedAt": _utcnow_iso(),
        "workflow": {
            "name": str(workflow.get("name") or "workflow"),
            "description": workflow.get("description") or None,
            "nodes": [dict(n) for n in nodes],
            "edges": [dict(e) for e in edges],
        },
    }

def envelope_to_json(envelope: dict) -> str:
    """Render the envelope as pretty-printed JSON (sorted keys for stable diffs)."""
    import json
    return json.dumps(envelope, indent=2, sort_keys=True, ensure_ascii=False)

# ─────────────────────────────────────────────────────────────────
# Deserialize / validate
# ─────────────────────────────────────────────────────────────────
def parse(payload: Any) -> dict:
    """Validate a JSON payload and return the inner workflow dict.

    Raises WorkflowSchemaError on any structural problem.
    """
    if not isinstance(payload, dict):
        raise WorkflowSchemaError("top-level value must be a JSON object")

    kind = payload.get("kind")
    if kind != KIND:
        raise WorkflowSchemaError(
            f"unsupported kind {kind!r} (expected {KIND!r})"
        )
    version = payload.get("schemaVersion")
    if not _version_supported(version):
        raise WorkflowSchemaError(
            f"unsupported schemaVersion {version!r} (this build supports {_supported_versions()})"
        )

    body = payload.get("workflow")
    if not isinstance(body, dict):
        raise WorkflowSchemaError("`workflow` must be an object")

    # — rewrite legacy node types (`parallel`, `steps`) to the
    # merged `flow` shape. Runs in place so downstream validation sees
    # the new types and the rest of the parse path is alias-free.
    migrate_envelope(body)

    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        raise WorkflowSchemaError("`workflow.name` must be a non-empty string")

    nodes = body.get("nodes") or []
    edges = body.get("edges") or []
    if not isinstance(nodes, list):
        raise WorkflowSchemaError("`workflow.nodes` must be an array")
    if not isinstance(edges, list):
        raise WorkflowSchemaError("`workflow.edges` must be an array")
    if not nodes:
        raise WorkflowSchemaError("`workflow.nodes` is empty — nothing to import")

    # Per-node validation
    seen_ids: set[str] = set()
    for i, n in enumerate(nodes):
        if not isinstance(n, dict):
            raise WorkflowSchemaError(f"node #{i} is not an object")
        nid = n.get("id")
        if not isinstance(nid, str) or not nid:
            raise WorkflowSchemaError(f"node #{i} missing `id`")
        if nid in seen_ids:
            raise WorkflowSchemaError(f"duplicate node id {nid!r}")
        seen_ids.add(nid)
        ntype = n.get("type")
        if not isinstance(ntype, str) or not ntype:
            raise WorkflowSchemaError(f"node {nid!r} missing `type`")
        # `type` is validated by the schemas.NODETYPES literal at create time.

    # Per-edge validation
    for i, e in enumerate(edges):
        if not isinstance(e, dict):
            raise WorkflowSchemaError(f"edge #{i} is not an object")
        for k in ("id", "source", "target"):
            v = e.get(k)
            if not isinstance(v, str) or not v:
                raise WorkflowSchemaError(f"edge #{i} missing {k!r}")
        if e["source"] not in seen_ids:
            raise WorkflowSchemaError(
                f"edge {e['id']!r} source {e['source']!r} is not a node"
            )
        if e["target"] not in seen_ids:
            raise WorkflowSchemaError(
                f"edge {e['id']!r} target {e['target']!r} is not a node"
            )

    # Run the same topo-sort / cycle check the generator does — catches
    # self-loops and disconnected subgraphs early.
    try:
        validate_workflow(nodes, edges)
    except GraphError as e:
        raise WorkflowSchemaError(f"graph validation failed: {e}") from e

    return {
        "name": name.strip(),
        "description": body.get("description") or None,
        "nodes": nodes,
        "edges": edges,
    }

# ─────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────
def _utcnow_iso() -> str:
    # -12T12:34:56.789123+00:00 → trim microseconds for readability
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return now.isoformat()

def _version_supported(v: Any) -> bool:
    # Only "1.x" for now; expand when the format changes.
    return isinstance(v, str) and v.startswith("1.")

def _supported_versions() -> str:
    return "1.x"
