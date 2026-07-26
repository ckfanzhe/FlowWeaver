"""Knowledge-source node emission — Python source for `knowledge` nodes.

Mirrors `tools_expr.py` but emits `kb_<nid> = Knowledge(...)` blocks
(parallel to the way `tool` nodes emit per-source wrappers). The block
ends up at module scope in the exported `.py` so agents can reference
`kb_<ref>` directly.

Construction order (matches `KnowledgeStrategy.build`):
  1. embedder = `<EmbedderClass>(...)`  — emits inline constructor.
  2. vector_db = `<VectorDbClass>(embedder=embedder, ...)` — embedder
     is constructed in line so we get a single readable block.
  3. knowledge = `Knowledge(vector_db=vector_db, name=..., max_results=...)`.

Returning everything as a single string means the compile pipeline
emits one block per `knowledge` node, in pass 0, before the agents.

`knowledge_ref(nid)` returns the variable expression (`kb_<nid>`) that
goes into `Agent(knowledge=...)` — same idiom as `tools_expr` returning
`<nid>_mcp`.
"""
from __future__ import annotations

from typing import Any

from .utils import q

# ─────────────────────────────────────────────────────────────────
# Per-backend / per-embedder constructor expressions
# ─────────────────────────────────────────────────────────────────
def _embedder_expr(cfg: dict) -> tuple[str, str]:
    """Return `(class_name, kwargs_text)` for the cfg's embedder.

    `class_name` is e.g. `"OpenAIEmbedder"`. `kwargs_text` is the
    comma-joined kwargs body for the inline constructor — already
    indented to match the surrounding block (no surrounding parens).

    Unknown embedder kinds raise `ValueError` — the LLM strict sibling
    validates this at save time, so by the runtime we're guaranteed a
    known kind or a Pydantic-rejected save.
    """
    kind = (cfg.get("embedder") or "openai").strip().lower()

    if kind == "openai":
        kwargs: list[str] = [f"id={q(cfg.get('openaiModel') or 'text-embedding-3-small')}"]
        if cfg.get("openaiApiKey"):
            kwargs.append(f"api_key={q(cfg['openaiApiKey'])}")
        if cfg.get("openaiBaseUrl"):
            kwargs.append(f"base_url={q(cfg['openaiBaseUrl'])}")
        if cfg.get("openaiDimensions"):
            kwargs.append(f"dimensions={int(cfg['openaiDimensions'])}")
        return "OpenAIEmbedder", ", ".join(kwargs)

    if kind == "sentence_transformers":
        kwargs = [
            f"model={q(cfg.get('sentenceTransformersModel') or 'sentence-transformers/all-MiniLM-L6-v2')}",
            f"dimensions={int(cfg.get('sentenceTransformersDimensions') or 384)}",
        ]
        return "SentenceTransformerEmbedder", ", ".join(kwargs)

    if kind == "cohere":
        kwargs = [
            f"model={q(cfg.get('cohereModel') or 'embed-english-v3.0')}",
            f"input_type={q(cfg.get('cohereInputType') or 'search_query')}",
        ]
        if cfg.get("cohereApiKey"):
            kwargs.append(f"api_key={q(cfg['cohereApiKey'])}")
        return "CohereEmbedder", ", ".join(kwargs)

    raise ValueError(
        f"knowledge_expr: unknown embedder {kind!r} "
        "(expected 'openai' | 'sentence_transformers' | 'cohere')"
    )


def _vector_db_expr(cfg: dict) -> tuple[str, str]:
    """Return `(class_name, kwargs_text)` for the cfg's vector DB.

    Mirrors `_embedder_expr`. The `embedder=...` kwarg is supplied by
    `knowledge_block` so the constructor body stays clean — here we
    only emit the backend-specific kwargs.
    """
    kind = (cfg.get("vectorDb") or "lancedb").strip().lower()

    if kind == "lancedb":
        kwargs = [
            f"uri={q(cfg.get('lancedbUri') or '/tmp/lancedb')}",
            f"table_name={q(cfg.get('lancedbTableName') or 'agno_kb')}",
        ]
        return "LanceDb", ", ".join(kwargs)

    if kind == "pgvector":
        kwargs = [
            f"db_url={q(cfg.get('pgvectorDbUrl') or '')}",
            f"table_name={q(cfg.get('pgvectorTableName') or 'agno_kb')}",
            f"schema={q(cfg.get('pgvectorSchema') or 'ai')}",
        ]
        return "PgVector", ", ".join(kwargs)

    if kind == "chroma":
        kwargs = [
            f"path={q(cfg.get('chromaPath') or './chroma_db')}",
            f"collection_name={q(cfg.get('chromaCollectionName') or 'agno_kb')}",
            f"persistent_client={'True' if cfg.get('chromaPersistentClient', True) else 'False'}",
        ]
        return "ChromaDb", ", ".join(kwargs)

    raise ValueError(
        f"knowledge_expr: unknown vectorDb {kind!r} "
        "(expected 'lancedb' | 'pgvector' | 'chroma')"
    )


# ─────────────────────────────────────────────────────────────────
# Block emitter — knowledge_block()
# ─────────────────────────────────────────────────────────────────
def knowledge_block(nid: str, node: dict, ctx: Any) -> str:
    """Emit the `kb_<nid> = Knowledge(...)` Python block.

    Output shape (one block per `knowledge` node, in pass 0):

        <nid>_embedder = OpenAIEmbedder(
            id="text-embedding-3-small",
        )
        <nid>_vector_db = LanceDb(
            uri="/tmp/lancedb",
            table_name="agno_kb",
            embedder=<nid>_embedder,
        )
        <nid>_kb = Knowledge(
            name="<name-or-empty>",
            vector_db=<nid>_vector_db,
            max_results=10,
        )

    Variable names use `<nid>_*` (rather than `_kb` / `_vector_db` /
    `_embedder` directly) so multiple knowledge nodes on the same
    canvas don't collide. Same convention as `tools_expr.iter_tool_function_blocks`
    which emits raw function defs.

    `_normalize_cfg` runs through `KnowledgeNodeConfig.model_validate`
    so default fields (vectorDb='lancedb', maxResults=10, …) are
    filled in. Mirrors `strategies/tool._normalize_cfg`.
    """
    cfg = _normalize_cfg((node.get("data") or {}).get("config") or {})

    embedder_cls, embedder_kwargs = _embedder_expr(cfg)
    vector_db_cls, vector_db_kwargs = _vector_db_expr(cfg)

    embedder_var = f"{nid}_embedder"
    vector_db_var = f"{nid}_vector_db"
    kb_var = knowledge_ref_name(nid)
    name_value = cfg.get("name") or nid

    parts: list[str] = []
    parts.append(
        f"{embedder_var} = {embedder_cls}(\n"
        f"    {embedder_kwargs},\n"
        f")\n"
    )
    parts.append(
        f"{vector_db_var} = {vector_db_cls}(\n"
        f"    {vector_db_kwargs},\n"
        f"    embedder={embedder_var},\n"
        f")\n"
    )
    parts.append(
        f"{kb_var} = Knowledge(\n"
        f"    name={q(name_value)},\n"
        f"    vector_db={vector_db_var},\n"
        f"    max_results={int(cfg.get('maxResults') or 10)},\n"
        f")\n"
    )
    return "".join(parts)


# ─────────────────────────────────────────────────────────────────
# Ref expression — knowledge_ref()
# ─────────────────────────────────────────────────────────────────
def knowledge_ref_name(nid: str) -> str:
    """The variable name emitted for a `knowledge` node's Knowledge instance.

    Mirrors `tools_expr.tools_expr(...)` returning `"<tref>_mcp"` etc.
    `Agent(knowledge=kb_<ref>)` is what the agent emitter uses.
    """
    return f"{nid}_kb"


def knowledge_ref(nid: str) -> str:
    """Return the Python expression for a knowledge node's variable.

    Same as `knowledge_ref_name` — separate name to match the
    `tools_expr.tools_expr` / `tools_list` API shape (callers can
    `expr = knowledge_ref(ref)` without remembering the suffix).
    """
    return knowledge_ref_name(nid)


# ─────────────────────────────────────────────────────────────────
# Imports helper — required_imports()
# ─────────────────────────────────────────────────────────────────
def required_imports(nodes_by_id: dict[str, dict]) -> list[str]:
    """Return the `from agno.X import Y` lines needed for the workflow's
    knowledge nodes. Empty list when no knowledge nodes are present.

    Mirrors `imports.collect_imports` for tools — the pipeline reads
    this and prepends the lines at the top of the exported `.py`.
    """
    kinds: set[str] = set()
    embedder_kinds: set[str] = set()
    for node in nodes_by_id.values():
        if node.get("type") != "knowledge":
            continue
        cfg = (node.get("data") or {}).get("config") or {}
        kinds.add((cfg.get("vectorDb") or "lancedb").strip().lower())
        embedder_kinds.add((cfg.get("embedder") or "openai").strip().lower())

    if not kinds and not embedder_kinds:
        return []

    out: list[str] = ["from agno.knowledge.knowledge import Knowledge"]
    if "lancedb" in kinds:
        out.append("from agno.vectordb.lancedb import LanceDb")
    if "pgvector" in kinds:
        out.append("from agno.vectordb.pgvector import PgVector")
    if "chroma" in kinds:
        out.append("from agno.vectordb.chroma import ChromaDb")
    if "openai" in embedder_kinds:
        out.append("from agno.knowledge.embedder.openai import OpenAIEmbedder")
    if "sentence_transformers" in embedder_kinds:
        out.append(
            "from agno.knowledge.embedder.sentence_transformer "
            "import SentenceTransformerEmbedder"
        )
    if "cohere" in embedder_kinds:
        out.append("from agno.knowledge.embedder.cohere import CohereEmbedder")
    return out


# ─────────────────────────────────────────────────────────────────
# Local helpers
# ─────────────────────────────────────────────────────────────────
def _normalize_cfg(cfg: dict) -> dict:
    """Lazily normalize the raw cfg via `KnowledgeNodeConfig`. Mirrors
    `strategies/knowledge._normalize_cfg` — both call sites need the
    same defaults + alias resolution."""
    from app.schemas.node_configs import KnowledgeNodeConfig
    return KnowledgeNodeConfig.model_validate(cfg).model_dump(by_alias=True)


__all__ = [
    "knowledge_block",
    "knowledge_ref",
    "knowledge_ref_name",
    "required_imports",
]
