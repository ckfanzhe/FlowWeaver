"""KnowledgeStrategy — `knowledge` node (RAG / retrieval source).

Mirror of [[strategies/tool.ToolStrategy]] but for agno's separate
`knowledge=...` parameter (NOT `tools=[...]`). Per the architectural
mirrors in plan [[gleaming-munching-grove]], this strategy:

  - declares `KIND="knowledge_source"` and `IS_KNOWLEDGE_SOURCE=True`
    so the compile pipeline's `_pass0_knowledge_sources` picks it up
  - `build()` constructs an `agno.knowledge.Knowledge(...)` instance
    and stashes it in `ctx.knowledge_objects[nid]` (added in Step 4)
  - `to_source()` emits the Python block
    (`kb_<nid> = Knowledge(vector_db=LanceDb(...), max_results=10)`)
    via `app.core.compile._helpers.knowledge_expr.knowledge_block`

The optional deps (`lancedb`, `chromadb`, `pgvector`,
`sentence-transformers`, `cohere`) are NOT installed by default.
`_build_vector_db` / `_build_embedder` wrap imports in
`try/except ImportError` and surface a clear compile error:
`knowledge node 'xxx': chromadb not installed — run: pip install chromadb`.
Keeps the lean default install.

Content ingestion is NOT auto-handled — the export emits only the
`Knowledge(...)` constructor. Users call `kb_<nid>.insert(path=...)` /
`insert(url=...)` / `insert(text_content=...)` themselves. See plan
[[gleaming-munching-grove]] §"Design Decisions" for rationale.
"""
from __future__ import annotations

import logging
from typing import Any, ClassVar, Literal, Optional

from .base import NodeStrategy

log = logging.getLogger(__name__)


def _normalize_cfg(cfg: dict) -> dict:
    """Run the raw config through `KnowledgeNodeConfig.model_validate`
    so Pydantic fills in defaults + aliases (the frontend sends
    camelCase). Returns the dumped dict so downstream reads
    (`cfg["vectorDb"]`, `cfg["openaiApiKey"]`, …) work uniformly.

    Mirrors `strategies/tool._normalize_cfg` — read-time lax
    (`_BASE_CONFIG`) so saved workflows load even when the schema has
    moved on. Future strict per-field validators hook in here.
    """
    from app.schemas.node_configs import KnowledgeNodeConfig
    return KnowledgeNodeConfig.model_validate(cfg).model_dump(by_alias=True)


# ─────────────────────────────────────────────────────────────────
# Optional-dep helpers
# ─────────────────────────────────────────────────────────────────
# All vector DB / embedder imports live behind try/except so the
# import of this module succeeds even when the user hasn't installed
# `lancedb` / `chromadb` / `pgvector` / `cohere` / etc. The actual
# `Knowledge(...)` construction happens at runtime inside `build()`
# — that's where the missing-dep error fires (loud, actionable).

def _require_module(modname: str, pip_name: str, *, nid: str) -> None:
    """Raise a clean compile error if `modname` isn't importable.

    `pip_name` is the name users should pass to `pip install` — often
    different from the import name (e.g. `chromadb` package →
    `chromadb` import; `pgvector` package → `pgvector` import).
    """
    try:
        __import__(modname)
    except ImportError as exc:
        raise RuntimeError(
            f"knowledge node {nid!r}: optional dependency "
            f"{pip_name!r} is not installed — run: "
            f"pip install {pip_name}"
        ) from exc


# ─────────────────────────────────────────────────────────────────
# Vector DB constructor dispatch — `_build_vector_db`
# ─────────────────────────────────────────────────────────────────
def _build_vector_db(cfg: dict, *, nid: str) -> Any:
    """Construct the agno vector DB instance from a flat cfg dict.

    Branches on `cfg["vectorDb"]`:
      - `'lancedb'`     → `LanceDb(uri=..., table_name=...)`
      - `'pgvector'`    → `PgVector(db_url=..., table_name=..., schema=...)`
      - `'chroma'`      → `ChromaDb(path=..., collection_name=..., persistent_client=...)`

    Missing optional deps surface as a clean `RuntimeError` with a
    `pip install <pkg>` hint. The embedder is NOT set here — it's
    injected by `_build_knowledge` after construction so we can use
    the embedder across `Knowledge(...)` and the backend if needed.
    """
    kind = (cfg.get("vectorDb") or "lancedb").strip().lower()

    if kind == "lancedb":
        _require_module("lancedb", "lancedb", nid=nid)
        from agno.vectordb.lancedb import LanceDb
        return LanceDb(
            uri=cfg.get("lancedbUri") or "/tmp/lancedb",
            table_name=cfg.get("lancedbTableName") or "agno_kb",
        )

    if kind == "pgvector":
        _require_module("pgvector", "pgvector", nid=nid)
        from agno.vectordb.pgvector import PgVector
        db_url = cfg.get("pgvectorDbUrl") or ""
        if not db_url:
            raise RuntimeError(
                f"knowledge node {nid!r}: vectorDb='pgvector' "
                "requires a non-empty pgvectorDbUrl"
            )
        return PgVector(
            db_url=db_url,
            table_name=cfg.get("pgvectorTableName") or "agno_kb",
            schema=cfg.get("pgvectorSchema") or "ai",
        )

    if kind == "chroma":
        _require_module("chromadb", "chromadb", nid=nid)
        from agno.vectordb.chroma import ChromaDb
        return ChromaDb(
            path=cfg.get("chromaPath") or "./chroma_db",
            collection_name=cfg.get("chromaCollectionName") or "agno_kb",
            persistent_client=bool(cfg.get("chromaPersistentClient", True)),
        )

    raise RuntimeError(
        f"knowledge node {nid!r}: unknown vectorDb {kind!r} "
        "(expected 'lancedb' | 'pgvector' | 'chroma')"
    )


# ─────────────────────────────────────────────────────────────────
# Embedder constructor dispatch — `_build_embedder`
# ─────────────────────────────────────────────────────────────────
def _build_embedder(cfg: dict, *, nid: str) -> Any:
    """Construct the agno embedder instance from a flat cfg dict.

    Branches on `cfg["embedder"]`:
      - `'openai'`               → `OpenAIEmbedder(id=..., api_key=..., base_url=..., dimensions=...)`
      - `'sentence_transformers'`→ `SentenceTransformerEmbedder(model=..., dimensions=...)`
      - `'cohere'`               → `CohereEmbedder(model=..., api_key=..., input_type=...)`

    Inline `*ApiKey` fields win; empty / None falls through to the
    matching env var (`OPENAI_API_KEY` / `COHERE_API_KEY`) handled
    inside each embedder class.
    """
    kind = (cfg.get("embedder") or "openai").strip().lower()

    if kind == "openai":
        _require_module("openai", "openai", nid=nid)
        from agno.knowledge.embedder.openai import OpenAIEmbedder
        kwargs: dict[str, Any] = {
            "id": cfg.get("openaiModel") or "text-embedding-3-small",
        }
        api_key = cfg.get("openaiApiKey")
        if api_key:
            kwargs["api_key"] = api_key
        base_url = cfg.get("openaiBaseUrl")
        if base_url:
            kwargs["base_url"] = base_url
        dims = cfg.get("openaiDimensions")
        if dims:
            kwargs["dimensions"] = int(dims)
        return OpenAIEmbedder(**kwargs)

    if kind == "sentence_transformers":
        _require_module(
            "agno.knowledge.embedder.sentence_transformer",
            "sentence-transformers",
            nid=nid,
        )
        from agno.knowledge.embedder.sentence_transformer import (
            SentenceTransformerEmbedder,
        )
        return SentenceTransformerEmbedder(
            model=(
                cfg.get("sentenceTransformersModel")
                or "sentence-transformers/all-MiniLM-L6-v2"
            ),
            dimensions=int(cfg.get("sentenceTransformersDimensions") or 384),
        )

    if kind == "cohere":
        _require_module(
            "agno.knowledge.embedder.cohere", "cohere", nid=nid
        )
        from agno.knowledge.embedder.cohere import CohereEmbedder
        kwargs = {
            "model": cfg.get("cohereModel") or "embed-english-v3.0",
            "input_type": cfg.get("cohereInputType") or "search_query",
        }
        api_key = cfg.get("cohereApiKey")
        if api_key:
            kwargs["api_key"] = api_key
        return CohereEmbedder(**kwargs)

    raise RuntimeError(
        f"knowledge node {nid!r}: unknown embedder {kind!r} "
        "(expected 'openai' | 'sentence_transformers' | 'cohere')"
    )


# ─────────────────────────────────────────────────────────────────
# KnowledgeStrategy — runtime + export
# ─────────────────────────────────────────────────────────────────
class KnowledgeStrategy(NodeStrategy):
    """Unified `knowledge` node — vector DB + embedder discriminators
    pick the concrete agno primitives at runtime.

    Architecture mirror of `ToolStrategy`:
      - `KIND='knowledge_source'` (parallel to `tool_source`)
      - `IS_KNOWLEDGE_SOURCE=True` → built in `_pass0_knowledge_sources`,
        stashed in `ctx.knowledge_objects[nid]`, wired to an agent by
        `_pass3_knowledge_wiring` via `Agent(knowledge=kb, ...)`.

    Content ingestion (sources[]) is intentionally NOT auto-handled —
    the export emits only the `Knowledge(...)` constructor; users
    call `kb_<nid>.insert(...)` themselves.
    """

    KIND: ClassVar[
        Literal["executable", "compound", "tool_source", "knowledge_source", "control_flow"]
    ] = "knowledge_source"
    COMPOUND_PASS: ClassVar[Optional[int]] = None
    IS_TOOL_SOURCE: ClassVar[bool] = False
    IS_KNOWLEDGE_SOURCE: ClassVar[bool] = True
    NEEDS_TOOL_WIRING: ClassVar[bool] = False
    NEEDS_KNOWLEDGE_WIRING: ClassVar[bool] = False
    STEP_WRAPPER: ClassVar[Literal["agent", "ask", "none"]] = "none"

    # ─────────────────────────────────────────────────────────────
    # build() — runtime construction (agno Knowledge instance)
    # ─────────────────────────────────────────────────────────────
    def build(self, nid: str, node: dict, ctx: Any) -> Any:
        """Build an agno `Knowledge` instance for one `knowledge` node.

        The returned object lands in `ctx.knowledge_objects[nid]` via
        the compile pipeline's `_pass0_knowledge_sources` pass. The
        `_pass3_knowledge_wiring` pass later attaches it to the agent
        that owns an incoming `knowledge_attachment` edge.

        Construction order matters for agno:
          1. `embedder = OpenAIEmbedder(...)`  — base class
          2. `vector_db = LanceDb(embedder=embedder)` — late-bind so
             `LanceDb.__post_init__` sees the embedder's `dimensions`
          3. `Knowledge(vector_db=...)` — calls `vector_db.create()` in
             `Knowledge.__post_init__`

        Reordering (e.g. constructing `Knowledge` first) raises on
        some backends because `dimensions` is unknown without the
        embedder.
        """
        from agno.knowledge.knowledge import Knowledge

        cfg = _normalize_cfg((node.get("data") or {}).get("config") or {})
        embedder = _build_embedder(cfg, nid=nid)
        vector_db = _build_vector_db(cfg, nid=nid)
        # Late-bind the embedder so the vector DB picks up its `dimensions`.
        try:
            vector_db.embedder = embedder
        except Exception:  # pragma: no cover — backend doesn't accept late-bind
            log.warning(
                "knowledge node %s: could not late-bind embedder on %s; "
                "vector DB may fall back to its default embedder",
                nid, type(vector_db).__name__,
            )

        return Knowledge(
            name=cfg.get("name") or nid,
            description=cfg.get("description") or None,
            vector_db=vector_db,
            max_results=int(cfg.get("maxResults") or 10),
        )

    # ─────────────────────────────────────────────────────────────
    # to_source() — code-gen (Python source emitted before wf.steps)
    # ─────────────────────────────────────────────────────────────
    def to_source(self, nid: str, node: dict, ctx: Any) -> str:
        """Emit the runtime Knowledge constructor as Python source.

        Delegates to `app.core.compile._helpers.knowledge_expr` so the
        build order (embedder → vector_db → Knowledge) is shared with
        any future emission sites (the runtime path is one; export is
        another).
        """
        from app.core.compile._helpers.knowledge_expr import knowledge_block
        return knowledge_block(nid, node, ctx)

    # ─────────────────────────────────────────────────────────────
    # build_tools() — N/A for knowledge nodes
    # ─────────────────────────────────────────────────────────────
    # Override the base's empty default to keep the contract obvious:
    # a knowledge node produces a `Knowledge` object, NOT agno tools.
    # Tool factories dispatch on `IS_TOOL_SOURCE=True` and skip us.
    def build_tools(  # noqa: D401 — simple override
        self,
        nid: str,
        ir_node: Any,
        ir_nodes: dict,
        *,
        user_id: Optional[str] = None,
    ) -> list:
        """Knowledge nodes produce no tools — kept here for symmetry
        with the base ABC. Returns an empty list."""
        return []

__all__ = [
    "KnowledgeStrategy",
    "_normalize_cfg",
    "_build_vector_db",
    "_build_embedder",
]
