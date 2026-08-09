"""KnowledgeStrategy — `knowledge` node (RAG / retrieval source).

Mirror of [[strategies/tool.ToolStrategy]] but for agno's separate
`knowledge=...` parameter (NOT `tools=[...]`). Per the architectural
mirrors in plan [[gleaming-munching-grove]], this strategy:

  - declares `KIND="knowledge_source"` and `IS_KNOWLEDGE_SOURCE=True`
    so the compile pipeline's `_pass0_knowledge_sources` picks it up
  - `build()` constructs an `agno.knowledge.Knowledge(...)` instance
    and stashes it in `ctx.knowledge_objects[nid]` (added in Step 4)
  - `to_source()` emits the Python block
    (`kb_<nid> = Knowledge(vector_db=PgVector(...), max_results=10)`)
    via `app.core.compile._helpers.knowledge_expr.knowledge_block`

v1 ships a single hard-coded backend stack (locked 2026-08-25):

  - `vector_db` → `PgVector(...)` (shares the docker-compose Postgres
    service `pgvector/pgvector:pg16`).
  - `embedder` → `OpenAIEmbedder(...)` (OpenAI / Azure / any OpenAI-
    compatible endpoint, configured via `openaiBaseUrl`).

The dispatch helpers (`_build_vector_db`, `_build_embedder`) collapse
to a single branch each — keeping the signature stable for forward-
compat (a future second backend widens the `Literal[...]` in the
schema and adds one `if kind == "..."` branch here). The compile
error path (`RuntimeError("unknown vectorDb ...")`) is the same shape
as before so callers don't need to special-case the v1 backend.

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
# Vector DB constructor — `_build_vector_db`
# ─────────────────────────────────────────────────────────────────
def _build_vector_db(cfg: dict, *, nid: str) -> Any:
    """Construct the agno vector DB instance from a flat cfg dict.

    v1 ships a single backend — `PgVector(...)` sharing the docker-
    compose Postgres. Missing dep surfaces as a clean `RuntimeError`
    with a `pip install <pkg>` hint. The embedder is NOT set here —
    it's injected by `_build_knowledge` after construction so we can
    use the embedder across `Knowledge(...)` and the backend if needed.
    """
    kind = (cfg.get("vectorDb") or "pgvector").strip().lower()

    if kind == "pgvector":
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

    raise RuntimeError(
        f"knowledge node {nid!r}: unknown vectorDb {kind!r} "
        "(v1 supports 'pgvector' only)"
    )


# ─────────────────────────────────────────────────────────────────
# Embedder constructor — `_build_embedder`
# ─────────────────────────────────────────────────────────────────
def _build_embedder(cfg: dict, *, nid: str) -> Any:
    """Construct the agno embedder instance from a flat cfg dict.

    v1 ships a single embedder — `OpenAIEmbedder(...)` against any
    OpenAI-compatible endpoint. `openaiApiKey` wins over the
    `OPENAI_API_KEY` env var; `openaiBaseUrl` is for Azure / self-
    hosted / vLLM proxies.
    """
    kind = (cfg.get("embedder") or "openai").strip().lower()

    if kind == "openai":
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

    raise RuntimeError(
        f"knowledge node {nid!r}: unknown embedder {kind!r} "
        "(v1 supports 'openai' only)"
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
          2. `vector_db = PgVector(embedder=embedder)` — late-bind so
             `PgVector.__post_init__` sees the embedder's `dimensions`
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