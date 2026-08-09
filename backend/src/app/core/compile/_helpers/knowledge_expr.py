"""Knowledge-source node emission — Python source for `knowledge` nodes.

Mirrors `tools_expr.py` but emits `kb_<nid> = Knowledge(...)` blocks
(parallel to the way `tool` nodes emit per-source wrappers). The block
ends up at module scope in the exported `.py` so agents can reference
`kb_<ref>` directly.

Construction order (matches `KnowledgeStrategy.build`):
  1. embedder = `OpenAIEmbedder(...)`  — emits inline constructor.
  2. vector_db = `PgVector(embedder=embedder, ...)` — embedder is
     constructed in line so we get a single readable block.
  3. knowledge = `Knowledge(vector_db=vector_db, name=..., max_results=...)`.

Returning everything as a single string means the compile pipeline
emits one block per `knowledge` node, in pass 0, before the agents.

`knowledge_ref(nid)` returns the variable expression (`kb_<nid>`) that
goes into `Agent(knowledge=...)` — same idiom as `tools_expr` returning
`<nid>_mcp`.

v1 ships a single backend stack (locked 2026-08-25): `PgVector` +
`OpenAIEmbedder`. The dispatch helpers (`_embedder_expr`,
`_vector_db_expr`) keep their `(class_name, kwargs_text)` return shape
so adding a future second backend is a one-line `if kind == "..."`
branch — the call sites (`knowledge_block`) are unchanged.
"""
from __future__ import annotations

from typing import Any

from .utils import q

# ─────────────────────────────────────────────────────────────────
# Per-backend / per-embedder constructor expressions
# ─────────────────────────────────────────────────────────────────
def _embedder_expr(cfg: dict) -> tuple[str, str]:
    """Return `(class_name, kwargs_text)` for the cfg's embedder.

    `class_name` is `"OpenAIEmbedder"`. `kwargs_text` is the
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

    raise ValueError(
        f"knowledge_expr: unknown embedder {kind!r} "
        "(v1 supports 'openai' only)"
    )


def _vector_db_expr(cfg: dict) -> tuple[str, str]:
    """Return `(class_name, kwargs_text)` for the cfg's vector DB.

    Mirrors `_embedder_expr`. The `embedder=...` kwarg is supplied by
    `knowledge_block` so the constructor body stays clean — here we
    only emit the backend-specific kwargs.
    """
    kind = (cfg.get("vectorDb") or "pgvector").strip().lower()

    if kind == "pgvector":
        kwargs = [
            f"db_url={q(cfg.get('pgvectorDbUrl') or '')}",
            f"table_name={q(cfg.get('pgvectorTableName') or 'agno_kb')}",
            f"schema={q(cfg.get('pgvectorSchema') or 'ai')}",
        ]
        return "PgVector", ", ".join(kwargs)

    raise ValueError(
        f"knowledge_expr: unknown vectorDb {kind!r} "
        "(v1 supports 'pgvector' only)"
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
        <nid>_vector_db = PgVector(
            db_url="...",
            table_name="agno_kb",
            schema="ai",
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
    so default fields (vectorDb='pgvector', maxResults=10, …) are
    filled in. Mirrors `strategies/knowledge._normalize_cfg`.
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

    v1 ships a fixed stack — `Knowledge` + `PgVector` + `OpenAIEmbedder` —
    so this is always the same 3-line block (when any knowledge node
    exists). Kept as a function for forward-compat (a future second
    backend widens this back into a discriminator dispatch).
    """
    has_knowledge = any(
        node.get("type") == "knowledge" for node in nodes_by_id.values()
    )
    if not has_knowledge:
        return []

    return [
        "from agno.knowledge.knowledge import Knowledge",
        "from agno.vectordb.pgvector import PgVector",
        "from agno.knowledge.embedder.openai import OpenAIEmbedder",
    ]


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