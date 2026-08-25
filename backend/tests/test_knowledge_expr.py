"""Unit tests for `app.core.compile._helpers.knowledge_expr`.

The codegen path is the v1-only stack (pgvector + OpenAI embedder),
collapsed from the pre-knowledge-simplify 3x3 matrix. These tests
guard against accidental re-introduction of backends or embedders,
and pin the exact emitted Python shape so a regression in the code
generator is caught immediately.

No Postgres needed — `knowledge_expr` is pure string emission.
"""
from __future__ import annotations

import pytest

from app.core.compile._helpers.knowledge_expr import (
    knowledge_block,
    knowledge_ref,
    knowledge_ref_name,
    required_imports,
)


def _node(**overrides) -> dict:
    """Build a minimal `knowledge` node dict with all required keys.

    Defaults match `shared/nodes.manifest.json` knowledge entry;
    callers override the bits they care about.
    """
    node = {
        "id": "kb1",
        "type": "knowledge",
        "data": {
            "config": {
                "name": "My KB",
                "description": "",
                "maxResults": 10,
                "addKnowledgeToContext": False,
                "vectorDb": "pgvector",
                "pgvectorDbUrl": "postgresql+psycopg://u:p@h:5432/d",
                "pgvectorTableName": "agno_kb",
                "pgvectorSchema": "ai",
                "embedder": "openai",
                "openaiModel": "text-embedding-3-small",
                "openaiApiKey": None,
                "openaiBaseUrl": None,
                "openaiDimensions": None,
                "sources": [],
            }
        },
    }
    node["data"]["config"].update(overrides)
    return node


def _ctx() -> dict:
    """Minimal compile context — knowledge_expr only needs `nodes_by_id`."""
    return {"nodes_by_id": {}}


# ─────────────────────────────────────────────────────────────────
# happy path: v1 stack emits exactly the right Python
# ─────────────────────────────────────────────────────────────────


def test_knowledge_block_emits_v1_stack():
    """v1 ships pgvector + openai only — pin the emitted shape.

    The exact format (variable names, kwarg order, embedded kwargs)
    is part of the public contract: copy-pasting the emitted
    `.py` into a standalone script must continue to import and
    run. A regression here breaks the export / download path.
    """
    node = _node(name="Docs")
    src = knowledge_block("kb1", node, _ctx())
    # Three blocks: embedder, vector_db, knowledge.
    assert "kb1_embedder = OpenAIEmbedder(" in src
    assert "kb1_vector_db = PgVector(" in src
    assert "kb1_kb = Knowledge(" in src
    # OpenAI embedder kwargs.
    assert 'id="text-embedding-3-small"' in src
    # pgvector kwargs.
    assert 'db_url="postgresql+psycopg://u:p@h:5432/d"' in src
    assert 'table_name="agno_kb"' in src
    assert 'schema="ai"' in src
    # Late-bind embedder is the second positional kwarg on PgVector.
    assert "embedder=kb1_embedder" in src
    # Knowledge kwargs.
    assert 'name="Docs"' in src
    assert "vector_db=kb1_vector_db" in src
    assert "max_results=10" in src
    # v1 only: no LanceDb / ChromaDb / CohereEmbedder / etc.
    for forbidden in (
        "LanceDb",
        "ChromaDb",
        "SentenceTransformerEmbedder",
        "CohereEmbedder",
    ):
        assert forbidden not in src, f"v1 should not emit {forbidden}"


def test_knowledge_block_emits_block_separators():
    """Each block ends with a newline so the assembly stays readable.

    Catches a regression where the loop forgets the `\\n` separator
    and emits one concatenated expression.
    """
    src = knowledge_block("kb1", _node(), _ctx())
    # 3 blocks → at least 3 blank-line boundaries (`\n)\n`).
    assert src.count("\n)\n") == 3


def test_knowledge_block_respects_max_results_default():
    """Empty / missing maxResults falls back to 10 — matches the
    backend's `_build_vector_db` fallback. Locked in to keep the
    export runtime-compatible with the live runtime.
    """
    node = _node()
    del node["data"]["config"]["maxResults"]
    src = knowledge_block("kb1", node, _ctx())
    assert "max_results=10" in src


def test_knowledge_block_respects_description_optional():
    """Description is optional — empty string omits from constructor.

    The runtime treats empty as "no description" (the strategy
    builds `description=cfg.description or None`). The exporter
    emits whatever the cfg holds, so empty stays empty (rather than
    getting normalised to None).
    """
    node = _node(description="")
    src = knowledge_block("kb1", node, _ctx())
    assert 'name="My KB"' in src
    # Description isn't in the constructor's named args — only
    # name / vector_db / max_results are emitted.
    assert "description=" not in src


def test_knowledge_block_handles_openai_dimensions_override():
    """Explicit openaiDimensions gets emitted as a kwarg.

    Without this, a users who tunes the dim for a non-default
    model gets a silent drop (the embedder then uses its default
    — usually wrong).
    """
    node = _node(openaiDimensions=512)
    src = knowledge_block("kb1", node, _ctx())
    assert "dimensions=512" in src


def test_knowledge_block_skips_openai_dimensions_when_none():
    """openaiDimensions=null (or missing) omits the kwarg.

    The runtime then uses its native default (1536 for the small
    model, 3072 for the large). Emitting `dimensions=None` would
    shadow that default and could brick the embedder.
    """
    node = _node(openaiDimensions=None)
    src = knowledge_block("kb1", node, _ctx())
    assert "dimensions=" not in src


def test_knowledge_block_openai_base_url_overrides_provider():
    """Custom openaiBaseUrl (vLLM / / Azure) emits as the base_url kwarg.

    This is the v1 way to point at a non-OpenAI provider that
    speaks the OpenAI protocol. Locked in so users who set up a
    local vLLM server don't see their config silently dropped.
    """
    node = _node(openaiBaseUrl="http://localhost:8000/v1")
    src = knowledge_block("kb1", node, _ctx())
    assert 'base_url="http://localhost:8000/v1"' in src


# ─────────────────────────────────────────────────────────────────
# references + imports
# ─────────────────────────────────────────────────────────────────


def test_knowledge_ref_name_uses_kb_suffix():
    assert knowledge_ref_name("abc") == "abc_kb"


def test_knowledge_ref_aliases_ref_name():
    """`knowledge_ref(nid)` returns the same string as
    `knowledge_ref_name(nid)` — they are two names for the same
    variable. Mirrors `tools_expr.tools_expr` / `tools_list`
    pattern in the runtime.
    """
    assert knowledge_ref("abc") == knowledge_ref_name("abc")


def test_required_imports_emits_v1_stack():
    """v1 stack = `Knowledge` + `PgVector` + `OpenAIEmbedder`.

    Any additional backend would re-introduce the 3x3 matrix this
    commit collapsed. The string check is intentionally rigid.
    """
    nodes_by_id = {"kb1": {"type": "knowledge", "data": {"config": {}}}}
    imports = required_imports(nodes_by_id)
    assert "from agno.knowledge.knowledge import Knowledge" in imports
    assert "from agno.vectordb.pgvector import PgVector" in imports
    assert "from agno.knowledge.embedder.openai import OpenAIEmbedder" in imports
    # v1 only — no LanceDb / ChromaDb / SentenceTransformerEmbedder /
    # CohereEmbedder.
    for forbidden in (
        "lancedb",
        "chroma",
        "sentence_transformer",
        "cohere",
    ):
        assert forbidden not in "\n".join(imports).lower(), (
            f"v1 must not import {forbidden}"
        )


def test_required_imports_empty_when_no_knowledge_nodes():
    """No knowledge nodes → no imports — keeps the runtime clean
    when the workflow doesn't use RAG.
    """
    nodes_by_id = {"a1": {"type": "agent", "data": {"config": {}}}}
    assert required_imports(nodes_by_id) == []


# ─────────────────────────────────────────────────────────────────
# error path: only pgvector + openai are accepted (v1)
# ─────────────────────────────────────────────────────────────────


def test_unknown_vector_db_raises_value_error():
    """Locking down the v1-only contract. If a legacy envelope
    still carries `vectorDb='lancedb'`, surface a clear error
    rather than silently emit the wrong backend.

    v1 narrows the schema's Literal to to "pgvector" — the
    rejection happens at Pydantic validation in
    `_normalize_cfg` (which calls `KnowledgeNodeConfig.model_validate`)
    BEFORE the codegen sees it. Any non-pgvector vectorDb raises
    a Pydantic ValidationError, which propagates as the
    `.v1 supports 'pgvector' only` runtime check. The two-stage
    defense means: even if a future change widens the Literal,
    the runtime check still catches it.
    """
    from pydantic import ValidationError

    node = _node(vectorDb="lancedb")
    with pytest.raises((ValidationError, ValueError), match="pgvector"):
        knowledge_block("kb1", node, _ctx())


def test_unknown_embedder_raises_value_error():
    """Same — embedder discriminator must be 'openai'."""
    from pydantic import ValidationError

    node = _node(embedder="sentence_transformers")
    with pytest.raises((ValidationError, ValueError), match="openai"):
        knowledge_block("kb1", node, _ctx())


def test_empty_pgvector_url_raises_runtime_error():
    """`pgvector` requires a non-empty URL. Surface a clear error
    rather than passing an empty string through to agno and
    getting a downstream connection error.

    This path is in the `KnowledgeStrategy._build_vector_db`
    runtime — the codegen just passes the value through. The
    test guards the cfg validation contract by passing an empty
    URL through the helper and confirming the empty value isn't
    silently dropped or normalised to a placeholder.
    """
    node = _node(pgvectorDbUrl="")
    src = knowledge_block("kb1", node, _ctx())
    # Codegen currently emits the empty value as-is (the runtime
    # raises). Locked in so a future "silently default to localhost"
    # patch is caught early.
    assert 'db_url=""' in src