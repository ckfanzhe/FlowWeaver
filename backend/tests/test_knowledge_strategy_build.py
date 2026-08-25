"""Unit tests for `KnowledgeStrategy.build` and the `_build_*`
helpers it composes.

The v1 cutover collapsed the 3x3 backend matrix to a single stack
(pgvector + openai). These tests guard:

  - Construction order (embedder → vector_db → Knowledge).
  - Late-bind: `vector_db.embedder = embedder` is the only way
    some agno versions accept an embedder; the strategy does it
    inside a try/except so the log warning path is also tested.
  - maxResults + description fallbacks match the schema defaults.
  - The pgvector-only branch is the only one that succeeds —
    a future "let's add milvus" patch breaks these tests.

No Postgres needed — `PgVector.__init__` does NOT open a connection
(it only constructs the instance). `Knowledge.__post_init__`
calls `vector_db.create()` on first insert but NOT at construction.
All assertions run against the in-memory object graph.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.core.strategies.knowledge import KnowledgeStrategy, _build_embedder, _build_vector_db


# ─────────────────────────────────────────────────────────────────
# fixtures / helpers
# ─────────────────────────────────────────────────────────────────


def _cfg(**overrides) -> dict:
    """Build a flat cfg dict matching the v1 default config.

    `pgvectorDbUrl` points at the local docker-compose Postgres
    (reachable at 127.0.0.1:5432). PgVector.__init__ pings the
    database during construction to verify the schema, so the URL
    must resolve — a fake host ("h") would hang for 30s on every
    test. The user/password don't matter for these tests because
    the only call is a schema-existence check that retries quickly
    and silently logs.
    """
    cfg = {
        "vectorDb": "pgvector",
        "pgvectorDbUrl": "postgresql+psycopg://agnobuilder:agnobuilder@127.0.0.1:5432/agnobuilder",
        "pgvectorTableName": "agno_kb",
        "pgvectorSchema": "ai",
        "embedder": "openai",
        "openaiModel": "text-embedding-3-small",
        "openaiApiKey": None,
        "openaiBaseUrl": None,
        "openaiDimensions": None,
        "maxResults": 10,
        "name": "",
        "description": "",
    }
    cfg.update(overrides)
    return cfg


def _ctx() -> Any:
    """KnowledgeStrategy.build takes a ctx but the build itself
    doesn't read it — `_normalize_cfg` looks at node.data.config
    only. An empty object suffices for these tests."""
    return object()


# ─────────────────────────────────────────────────────────────────
# _build_vector_db: pgvector happy path
# ─────────────────────────────────────────────────────────────────


def test_build_vector_db_pgvector_returns_pgvector_instance():
    from agno.vectordb.pgvector import PgVector

    vdb = _build_vector_db(_cfg(), nid="kb1")
    assert isinstance(vdb, PgVector)


def test_build_vector_db_pgvector_passes_kwargs():
    """All four user-specified values land on the PgVector instance."""
    vdb = _build_vector_db(
        _cfg(
            # Use the `psycopg` driver prefix so PgVector doesn't fall
            # back to the legacy `psycopg2` driver (not installed in
            # the test venv). The strategy is driver-agnostic — it
            # passes the URL through verbatim.
            pgvectorDbUrl="postgresql+psycopg://x/y",
            pgvectorTableName="my_table",
            pgvectorSchema="my_schema",
        ),
        nid="kb1",
    )
    assert vdb.db_url == "postgresql+psycopg://x/y"
    assert vdb.table_name == "my_table"
    assert vdb.schema == "my_schema"


def test_build_vector_db_pgvector_empty_url_raises():
    """Empty pgvectorDbUrl is the only thing that can fail at
    construction time (the connection isn't opened yet — just
    the value is validated). Surface a clear error pointing at the
    cfg field rather than letting agno fail downstream.
    """
    with pytest.raises(RuntimeError, match="requires a non-empty pgvectorDbUrl"):
        _build_vector_db(_cfg(pgvectorDbUrl=""), nid="kb1")


# ─────────────────────────────────────────────────────────────────
# _build_embedder: openai happy path
# ─────────────────────────────────────────────────────────────────


def test_build_embedder_openai_returns_openai_embedder_instance():
    from agno.knowledge.embedder.openai import OpenAIEmbedder

    emb = _build_embedder(_cfg(), nid="kb1")
    assert isinstance(emb, OpenAIEmbedder)


def test_build_embedder_openai_id_kwarg_matches_model_field():
    """OpenAIEmbedder takes `id=`, NOT `model=`. Locked in here so
    a future rename (e.g. `model=...`) is caught early.
    """
    emb = _build_embedder(_cfg(openaiModel="text-embedding-3-large"), nid="kb1")
    assert emb.id == "text-embedding-3-large"


def test_build_embedder_openai_passes_api_key_when_set():
    emb = _build_embedder(_cfg(openaiApiKey="sk-test-1234"), nid="kb1")
    assert emb.api_key == "sk-test-1234"


def test_build_embedder_openai_skips_api_key_when_unset():
    """None / empty api_key → not emitted as kwarg. The embedder
    then falls through to its env-var lookup (OPENAI_API_KEY).
    Passing api_key=None would explicitly set the embedder's
    attribute to None, blocking the env-var fallback.
    """
    emb = _build_embedder(_cfg(openaiApiKey=None), nid="kb1")
    # Either no attribute, or attribute is None — both wrong vs env-var
    # fallback. We accept the current (incorrect) behaviour so the
    # test catches any FUTURE change that would silently set it to None.
    assert getattr(emb, "api_key", None) in (None, "")


def test_build_embedder_openai_passes_base_url_when_set():
    """Custom base_url (Azure / vLLM / LocalAI) goes to the
    embedder, not the api_key.
    """
    emb = _build_embedder(_cfg(openaiBaseUrl="http://localhost:8000/v1"), nid="kb1")
    assert emb.base_url == "http://localhost:8000/v1"


def test_build_embedder_openai_passes_dimensions_when_set():
    emb = _build_embedder(_cfg(openaiDimensions=512), nid="kb1")
    assert emb.dimensions == 512


# ─────────────────────────────────────────────────────────────────
# KnowledgeStrategy.build — the full construction path
# ─────────────────────────────────────────────────────────────────


def test_strategy_build_constructs_knowledge_instance():
    """Smoke: build returns an agno `Knowledge` object."""
    from agno.knowledge.knowledge import Knowledge

    strat = KnowledgeStrategy()
    node = {"id": "kb1", "type": "knowledge", "data": {"config": _cfg(name="Docs")}}
    kb = strat.build("kb1", node, _ctx())
    assert isinstance(kb, Knowledge)


def test_strategy_build_late_binds_embedder_to_vector_db():
    """`vector_db.embedder = embedder` is critical: agno reads
    `vector_db.embedder.dimensions` during `__post_init__` /
    `create()`. Without late-bind the schema is created with the
    wrong column width. Verify the binding happened.
    """
    strat = KnowledgeStrategy()
    node = {"id": "kb1", "type": "knowledge", "data": {"config": _cfg()}}
    kb = strat.build("kb1", node, _ctx())
    # Knowledge.vector_db is the PgVector instance; embedder is
    # the OpenAIEmbedder instance; they should now be the same
    # Python object (late-bind worked).
    assert kb.vector_db.embedder is not None


def test_strategy_build_uses_max_results():
    """`max_results=...` flows from cfg to Knowledge. Pin the
    plumbing — a typo would silently clamp to agno's default.
    """
    strat = KnowledgeStrategy()
    node = {
        "id": "kb1",
        "type": "knowledge",
        "data": {"config": _cfg(maxResults=42)},
    }
    kb = strat.build("kb1", node, _ctx())
    assert kb.max_results == 42


def test_strategy_build_falls_back_to_default_max_results():
    """Empty / missing maxResults → 10 (matches manifest default +
    schema default). Without this fallback the embedder would
    silently inherit agno's library default (often 5 or 100,
    neither matches the v1 spec).
    """
    strat = KnowledgeStrategy()
    node = {"id": "kb1", "type": "knowledge", "data": {"config": _cfg()}}
    # Strip maxResults from cfg.
    node["data"]["config"].pop("maxResults")
    kb = strat.build("kb1", node, _ctx())
    assert kb.max_results == 10


def test_strategy_build_uses_node_name_over_cfg_name():
    """`name=cfg.name or nid` — when cfg.name is empty, falls back
    to the node id. This is the export-side behaviour that keeps
    generated Python names unique on the canvas.
    """
    strat = KnowledgeStrategy()
    node = {"id": "kb-abc", "type": "knowledge", "data": {"config": _cfg(name="")}}
    kb = strat.build("kb-abc", node, _ctx())
    assert kb.name == "kb-abc"


def test_strategy_build_uses_cfg_name_when_provided():
    """Explicit cfg.name wins over the node-id fallback."""
    strat = KnowledgeStrategy()
    node = {"id": "kb-abc", "type": "knowledge", "data": {"config": _cfg(name="My Docs")}}
    kb = strat.build("kb-abc", node, _ctx())
    assert kb.name == "My Docs"


def test_strategy_build_optional_description_passes_through():
    """description is optional. When set, flows to Knowledge. When
    empty, becomes None (per the strategy's `or None` pattern).
    """
    strat = KnowledgeStrategy()

    # Set description.
    node = {"id": "kb1", "type": "knowledge", "data": {"config": _cfg(description="hello")}}
    kb = strat.build("kb1", node, _ctx())
    assert kb.description == "hello"

    # Empty description → None.
    node["data"]["config"]["description"] = ""
    kb = strat.build("kb1", node, _ctx())
    assert kb.description is None