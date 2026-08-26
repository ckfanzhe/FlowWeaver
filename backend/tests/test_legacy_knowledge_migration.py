"""Regression tests for the v1 knowledge simplification read path.

Bug context: 2026-08-26 user-reported runtime error. After the
knowledge-node simplification collapsed the 3x3 backend matrix to
`pgvector + openai`, `KnowledgeNodeConfig.vectorDb` / `.embedder`
became `Literal['pgvector']` / `Literal['openai']`. Old DB rows
saved with `vectorDb='lancedb'` (or `'chroma'`, etc.) crashed on
GET with:

    pydantic_core._pydantic_core.ValidationError: 1 validation
    error for WorkflowRead
      nodes.0.vectorDb
        Input should be 'pgvector' [type=literal_error,
        input_value='lancedb']

Fix: `_compat.migrate_node_dict` now rewrites legacy
`vectorDb` / `embedder` values in-place on every read
(`_validate_node_type` always runs the migration; it's idempotent
on already-migrated values).

These tests guard the fix end-to-end via the WorkflowNode
Pydantic validator (the same path the API hits on every GET).
"""
from __future__ import annotations

import pytest

from app.core._compat import (
    LEGACY_KNOWLEDGE_CONFIG_REWRITES,
    migrate_envelope,
    migrate_node_dict,
)
from app.schemas.workflow import WorkflowNode


# ─────────────────────────────────────────────────────────────────
# Pure-function level: `migrate_node_dict` rewrites the values
# ─────────────────────────────────────────────────────────────────


def test_migrate_knowledge_lancedb_to_pgvector():
    """`vectorDb='lancedb'` → `'pgvector'`. The exact case the user
    hit in production. Without the migration, every GET on a
    pre-cutover workflow 500s."""
    node = {
        "id": "kb1",
        "type": "knowledge",
        "data": {"config": {"vectorDb": "lancedb"}},
    }
    migrate_node_dict(node)
    assert node["data"]["config"]["vectorDb"] == "pgvector"


def test_migrate_knowledge_chroma_to_pgvector():
    """`vectorDb='chroma'` → `'pgvector'`. Same shape, second
    legacy backend. Pinned so a future patch that only handles
    `lancedb` is caught."""
    node = {
        "id": "kb1",
        "type": "knowledge",
        "data": {"config": {"vectorDb": "chroma"}},
    }
    migrate_node_dict(node)
    assert node["data"]["config"]["vectorDb"] == "pgvector"


def test_migrate_knowledge_sentence_transformers_to_openai():
    """`embedder='sentence_transformers'` → `'openai'`."""
    node = {
        "id": "kb1",
        "type": "knowledge",
        "data": {"config": {"embedder": "sentence_transformers"}},
    }
    migrate_node_dict(node)
    assert node["data"]["config"]["embedder"] == "openai"


def test_migrate_knowledge_cohere_to_openai():
    """`embedder='cohere'` → `'openai'`."""
    node = {
        "id": "kb1",
        "type": "knowledge",
        "data": {"config": {"embedder": "cohere"}},
    }
    migrate_node_dict(node)
    assert node["data"]["config"]["embedder"] == "openai"


def test_migrate_knowledge_both_legacy_at_once():
    """Combined legacy values: both discriminators rewritten in
    one pass. The user's DB row had this exact shape."""
    node = {
        "id": "kb1",
        "type": "knowledge",
        "data": {
            "config": {
                "vectorDb": "lancedb",
                "embedder": "sentence_transformers",
            }
        },
    }
    migrate_node_dict(node)
    assert node["data"]["config"]["vectorDb"] == "pgvector"
    assert node["data"]["config"]["embedder"] == "openai"


def test_migrate_knowledge_idempotent_on_v1_values():
    """v1-only values (`pgvector`, `openai`) pass through unchanged.
    Idempotent — calling `migrate_node_dict` on every read costs
    ~one dict lookup per knowledge node, no schema rewrite."""
    node = {
        "id": "kb1",
        "type": "knowledge",
        "data": {
            "config": {"vectorDb": "pgvector", "embedder": "openai"},
        },
    }
    migrate_node_dict(node)
    assert node["data"]["config"]["vectorDb"] == "pgvector"
    assert node["data"]["config"]["embedder"] == "openai"


def test_migrate_knowledge_is_noop_for_non_knowledge_nodes():
    """`agent` / / `branch` / etc. nodes don't carry `vectorDb`
    or `embedder` discriminators — the migration must be a no-op
    for them. Pinned so a future "always rewrite" patch doesn't
    silently overwrite unrelated config keys."""
    node = {
        "id": "a1",
        "type": "agent",
        "data": {"config": {"model": "gpt-4o"}},
    }
    snapshot = dict(node["data"]["config"])
    migrate_node_dict(node)
    assert node["data"]["config"] == snapshot


def test_legacy_rewrite_map_is_complete():
    """Pinned the rewrite map's contents. Adding a new legacy
    backend (e.g. `qdrant`) requires updating both this test AND
    the migration map. The asymmetry between Pydantic's narrowed
    Literal and the migration map would silently let new backends
    slip through."""
    assert set(LEGACY_KNOWLEDGE_CONFIG_REWRITES["vectorDb"].keys()) == {
        "lancedb", "chroma", "pgvector",
    }
    assert set(LEGACY_KNOWLEDGE_CONFIG_REWRITES["embedder"].keys()) == {
        "sentence_transformers", "cohere", "openai",
    }


# ─────────────────────────────────────────────────────────────────
# Envelope-level: `migrate_envelope` walks every node
# ─────────────────────────────────────────────────────────────────


def test_migrate_envelope_rewrites_all_legacy_nodes():
    """`migrate_envelope` is what `workflow_io.parse` calls on
    import. It walks every node and applies `migrate_node_dict`,
    so a multi-node envelope with mixed legacy/new shapes all
    get rewritten in one pass."""
    envelope = {
        "schemaVersion": "2.0",
        "nodes": [
            {
                "id": "kb1",
                "type": "knowledge",
                "data": {"config": {"vectorDb": "lancedb"}},
            },
            {
                "id": "kb2",
                "type": "knowledge",
                "data": {"config": {"vectorDb": "chroma", "embedder": "cohere"}},
            },
            {
                "id": "a1",
                "type": "agent",
                "data": {"config": {"model": "gpt-4o"}},
            },
        ],
    }
    migrate_envelope(envelope)
    assert envelope["nodes"][0]["data"]["config"]["vectorDb"] == "pgvector"
    assert envelope["nodes"][1]["data"]["config"]["vectorDb"] == "pgvector"
    assert envelope["nodes"][1]["data"]["config"]["embedder"] == "openai"
    # Non-knowledge nodes untouched.
    assert envelope["nodes"][2]["data"]["config"] == {"model": "gpt-4o"}


# ─────────────────────────────────────────────────────────────────
# End-to-end: WorkflowNode validator accepts legacy values
# (the same path the API hits on every GET)
# ─────────────────────────────────────────────────────────────────


def test_workflow_node_validator_accepts_legacy_lancedb():
    """`WorkflowNode(**legacy_node_dict)` must NOT raise
    `ValidationError`. Before the fix this raised
    `nodes.0.vectorDb Input should be 'pgvector'`, causing the
    API to 500 on every GET against a pre-cutover workflow."""
    node = WorkflowNode(
        id="kb1",
        type="knowledge",
        position={"x": 0.0, "y": 0.0},
        data={
            "config": {
                "name": "Docs",
                "vectorDb": "lancedb",
                "embedder": "sentence_transformers",
            }
        },
    )
    cfg = node.data["config"]
    # The rewrite happens BEFORE validation, so the model carries
    # the post-migration shape — confirming the round-trip.
    assert cfg["vectorDb"] == "pgvector"
    assert cfg["embedder"] == "openai"


def test_workflow_node_validator_accepts_legacy_chroma():
    """Second legacy backend (`chroma`). The bug report only
    mentioned `lancedb`, but `chroma` would have hit the same
    ValidationError — pinned here for symmetry."""
    node = WorkflowNode(
        id="kb1",
        type="knowledge",
        position={"x": 0.0, "y": 0.0},
        data={"config": {"vectorDb": "chroma", "embedder": "cohere"}},
    )
    assert node.data["config"]["vectorDb"] == "pgvector"
    assert node.data["config"]["embedder"] == "openai"


def test_workflow_node_validator_preserves_v1_values():
    """v1-only values pass through without modification. Pinned
    so a future "always coerce" patch that drops the rewrite's
    idempotency is caught."""
    node = WorkflowNode(
        id="kb1",
        type="knowledge",
        position={"x": 0.0, "y": 0.0},
        data={
            "config": {
                "vectorDb": "pgvector",
                "embedder": "openai",
            }
        },
    )
    assert node.data["config"]["vectorDb"] == "pgvector"
    assert node.data["config"]["embedder"] == "openai"


def test_workflow_node_validator_raises_on_genuinely_unknown_value():
    """The migration is NOT a catch-all — genuinely unknown
    legacy values (e.g. `qdrant` added by a future user) MUST
    still raise so the schema's strict Literal stays meaningful.
    The migration only handles the values it knows about; any
    other string falls through to the strict validator."""
    with pytest.raises((ValueError, Exception)) as exc_info:
        WorkflowNode(
            id="kb1",
            type="knowledge",
            position={"x": 0.0, "y": 0.0},
            data={"config": {"vectorDb": "qdrant"}},
        )
    # We don't pin the exact exception class — Pydantic raises
    # ValidationError but the message is what matters. Confirm
    # `qdrant` appears in the error so we know the strict
    # validator fired (NOT silently coerced).
    assert "qdrant" in str(exc_info.value) or "vectorDb" in str(exc_info.value)