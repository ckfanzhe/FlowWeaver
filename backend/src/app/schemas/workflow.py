"""Pydantic schemas for workflow CRUD + runtime payloads.

Mirrors frontend/src/types/workflow.ts. Changes on either side MUST be reflected.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app.schemas.node_configs import validate_node_config

# ─────────────────────────────────────────────────────────────────
# Node types — manifest-driven.
#
# Earlier this file hardcoded a `Literal[...]` of the 9 base types.
# That broke autosave for workflows containing preset types
# (wikipedia / brave_search / tavily / open_meteo / coingecko /
# frankfurter) — the manifest declares them via `extends:` chains,
# but Pydantic's static Literal couldn't see them and 422'd every
# save.
#
# The new shape is intentionally permissive: `type` is `str`, and
# `WorkflowNode._validate_node_type` rejects unknown values against
# the manifest registry (see `app.core.node_types.NODE_TYPES`). This
# keeps the manifest the single source of truth — adding a new preset
# in `shared/nodes.manifest.json` automatically extends what
# workflows can carry, no schema edit required.
#
# `NODE_TYPES` (the tuple) is kept as a backwards-compatible alias
# for any consumer that still iterates the legacy 9-type list. New
# code should call `app.core.node_types.PALETTE_ORDER` instead.
# ─────────────────────────────────────────────────────────────────
NodeType = str

# Legacy constant — pre-Phase-9 callers may still import this.
# Tests that iterate "every supported node type" should use
# `app.core.node_types.PALETTE_ORDER` (now 14 entries incl. presets).
# `parallel`+`steps` collapsed to `flow`.
NODE_TYPES: tuple[str, ...] = (
    "agent", "tool",
    "branch", "flow", "loop", "ask",
)

# ─────────────────────────────────────────────────────────────────
# Graph primitives
#
# `WorkflowNode.data.config` is dispatched through the per-type schema
# registry at validation time — see `app.schemas.node_configs`. Bad
# shapes (missing `instructions`, `loop.maxIterations > 1000`, etc.)
# surface as a 422 here instead of failing at runtime / code export.
# ─────────────────────────────────────────────────────────────────
class WorkflowNode(BaseModel):
    id: str
    type: NodeType
    position: dict[str, float] = Field(default_factory=lambda: {"x": 0.0, "y": 0.0})
    data: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_node_type(self) -> "WorkflowNode":
        """Reject node types that aren't in the manifest.

        The manifest is the source of truth — it includes the 9 base
        types AND every preset declared via `extends:` (wikipedia,
        brave_search, etc.). A literal here would be brittle: each
        new preset would need a schema edit.

        — legacy aliases (`parallel`, `steps`) are rewritten
        in-place to the merged `flow` type BEFORE the registry check,
        so workflows stored with the old types keep loading. The
        rewrite injects `config.mode` so downstream strategy
        dispatch picks the right runtime primitive.

        The check runs against the cached registry so the cost is
        one dict lookup per node. `_validate_config` (below) still
        runs and is what enforces the per-type config shape.
        """
        # Lazy import: avoids a top-level cycle
        # (workflow.py → node_types → … → workflow.py).
        from app.core._compat import migrate_node_dict
        from app.core.node_types import NODE_TYPES as _REGISTRY

        if self.type not in _REGISTRY:
            # Legacy alias path: rewrite in place via the migration
            # layer, then re-check. We mutate `self.data` (a dict the
            # caller owns) so the same object the validator sees is
            # what subsequent generations of the Pydantic model carry.
            node_dict = {"type": self.type, "data": self.data}
            migrate_node_dict(node_dict)
            if node_dict["type"] != self.type:
                self.type = node_dict["type"]
            if self.type not in _REGISTRY:
                raise ValueError(
                    f"unknown node type {self.type!r}; known types: "
                    f"{sorted(_REGISTRY)}"
                )
        return self

    @model_validator(mode="after")
    def _validate_config(self) -> "WorkflowNode":
        """Re-validate `data.config` against the per-type schema.

        Two passes: we first parse `data` so the validator sees the
        shape it expects (`{label?, config}`), then replace
        `data.config` with a typed object so downstream consumers
        (the generator, the executor) get strict shapes back.
        """
        if not isinstance(self.data, dict):
            return self
        cfg = self.data.get("config")
        if isinstance(cfg, dict):
            validated = validate_node_config(self.type, cfg)
            # Mutate via dict reconstruction so `model_config` extra="ignore"
            # doesn't drop the typed object on the next pass.
            new_data = dict(self.data)
            new_data["config"] = (
                validated.model_dump(by_alias=True)
                if hasattr(validated, "model_dump")
                else validated
            )
            self.data = new_data
        return self

class WorkflowEdge(BaseModel):
    id: str
    source: str
    target: str
    sourceHandle: Optional[str] = None
    targetHandle: Optional[str] = None
    # Edge kind. The workflow canvas carries three distinct edge
    # semantics. `dataflow` (default) is the existing control-flow
    # edge; `tool_attachment` is the `tool-source → agent` wiring that
    # replaced cfg.toolsRef; `knowledge_attachment` is the RAG
    # `knowledge → agent` wiring that binds a Knowledge instance to an
    # agent's `knowledge=...` parameter. New in
    # [[gleaming-munching-grove]]. Unknown / absent values are coerced
    # to `dataflow` by the validator.
    kind: Optional[Literal["dataflow", "tool_attachment", "knowledge_attachment"]] = None

# ─────────────────────────────────────────────────────────────────
# Workflow CRUD
# ─────────────────────────────────────────────────────────────────
class WorkflowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = None
    nodes: list[WorkflowNode] = Field(default_factory=list)
    edges: list[WorkflowEdge] = Field(default_factory=list)

class WorkflowUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    nodes: Optional[list[WorkflowNode]] = None
    edges: Optional[list[WorkflowEdge]] = None

class WorkflowImport(BaseModel):
    """Accepts a JSON envelope produced by `workflow_io.serialize`.

    The frontend is expected to send the full envelope (schemaVersion +
    kind + workflow). The backend re-validates via `workflow_io.parse`.
    """
    payload: dict

class WorkflowRead(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    nodes: list[WorkflowNode]
    edges: list[WorkflowEdge]
    isTemplate: bool = False
    category: Optional[str] = None
    # Locale tag. The seed reads this from the JSON's
    # `locale` field and stores it on the row so the API can return it
    # without re-parsing the JSON files. Defaults to `"en"` so legacy
    # rows that pre-date the column still project cleanly.
    locale: str = "en"
    createdAt: datetime
    updatedAt: datetime

    @classmethod
    def from_orm_row(cls, row) -> "WorkflowRead":
        return cls(
            id=row.id,
            name=row.name,
            description=row.description,
            nodes=row.nodes or [],
            edges=row.edges or [],
            isTemplate=bool(getattr(row, "is_template", False)),
            category=getattr(row, "category", None),
            locale=getattr(row, "locale", None) or "en",
            createdAt=row.created_at,
            updatedAt=row.updated_at,
        )

class WorkflowSummary(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    isTemplate: bool = False
    category: Optional[str] = None
    locale: str = "en"
    createdAt: datetime
    updatedAt: datetime

    @classmethod
    def from_orm_row(cls, row) -> "WorkflowSummary":
        return cls(
            id=row.id,
            name=row.name,
            description=row.description,
            isTemplate=bool(getattr(row, "is_template", False)),
            category=getattr(row, "category", None),
            locale=getattr(row, "locale", None) or "en",
            createdAt=row.created_at,
            updatedAt=row.updated_at,
        )

class TemplateSummary(BaseModel):
    """Lightweight projection of a built-in template for the gallery view.

    `nodeTypes` is the deduplicated list of node types the template uses
    (e.g. `["agent", "router", "agent"]`) so the frontend can render
    colored chips WITHOUT downloading the full node/edge JSON. The full
    `WorkflowRead` is fetched only when the user clicks a template
    (instantiate).

    `locale` (added ) is the language tag the frontend uses
    to filter the gallery (`en` vs `zh-CN`). Defaults to `"en"` so
    legacy rows that pre-date the column still project cleanly.
    """
    id: str
    name: str
    description: Optional[str] = None
    category: Optional[str] = None
    isTemplate: bool = True
    locale: str = "en"
    nodeTypes: list[NodeType]
    nodeCount: int
    edgeCount: int

    @classmethod
    def from_orm_row(cls, row) -> "TemplateSummary":
        nodes = row.nodes or []
        edges = row.edges or []
        seen: list[str] = []
        for n in nodes:
            t = n.get("type") if isinstance(n, dict) else None
            if t and t not in seen:
                seen.append(t)
        return cls(
            id=row.id,
            name=row.name,
            description=row.description,
            category=getattr(row, "category", None),
            isTemplate=bool(getattr(row, "is_template", False)),
            locale=getattr(row, "locale", None) or "en",
            nodeTypes=seen,  # type: ignore[arg-type]
            nodeCount=len(nodes),
            edgeCount=len(edges),
        )

# ─────────────────────────────────────────────────────────────────
# Runtime payloads
# ─────────────────────────────────────────────────────────────────
class RunRequest(BaseModel):
    workflow_id: str
    input: str
    session_id: Optional[str] = None

class RunFromRequest(BaseModel):
    """Re-run a workflow but start execution at a specific node instead of
    the entry point. Used by the trace panel's "Re-run from here" button.

    `start_node_id` must exist in the workflow's node list. The original
    `input` is preserved and the session is reset (status back to
    `running`); any prior history is discarded.
    """
    workflow_id: str
    input: str
    start_node_id: str

class ContinueRequest(BaseModel):
    session_id: str
    response: Any  # str for text/choice, bool for confirm
    kind: Optional[Literal["tool_confirm", "ask"]] = None  # disambiguate when multiple pending