"""NodeStrategy ABC — one class per node type.

A `NodeStrategy` owns the runtime compilation AND the source emission
for one node kind. The class attributes are declarative metadata
mirroring the manifest's `kind` / `capabilities` block so a consumer
that holds a `NodeStrategy` instance can decide "is this a compound
type?" / "does this node go through pass 3?" without consulting the
manifest separately.

Why classvars and not just `NodeTypeSpec`:

    * The manifest is JSON; we want to test strategies in isolation
      without booting the manifest loader.
    * New node types that bypass the manifest (e.g. tests) can still
      declare capabilities via the subclass.
    * The manifest loader reads the classvars when synthesising the
      spec, so both paths converge on the same shape.

After .A  every manifest row's `runtime.builder`
resolves to a concrete subclass here; one class owns both `build()`
(runtime) and `to_source()` (export) inline. No legacy bridge, no
emitter-module layer.

Adding a new node type is now a one-class change. The manifest row
names the class; the pipeline + serializer look it up via
`resolve_strategy(name)` (see `strategies/__init__.py`). No edits to
`compile/pipeline.py`, `compile/serialize.py`, or `tool_factories.py`
for types that follow the pattern.
"""
from __future__ import annotations

from typing import Any, ClassVar, Literal, Optional

class NodeStrategy:
    """Base class for per-node-type behaviour.

    Subclasses are expected to be cheap to instantiate (no I/O, no
    DB, no LLM calls in `__init__`). The pipeline instantiates one
    strategy per node type at startup (cached) and reuses the
    instance for every node of that type.

    Concrete subclasses override `build()` for runtime objects and
    `to_source()` for source emission. Tool-source types also
    implement `build_tools()` (used by `tool_factories.build_tools_for_node`).
    Default implementations raise so a subclass that forgets to
    implement one of them surfaces immediately at the first
    workflow run — not silently with a missing method.

    ClassVar defaults mirror the manifest's "executable, no wiring"
    baseline — the most common case is a one-line class with just
    `build()` / `to_source()`. Override only what differs.
    """

    # ─────────────────────────────────────────────────────────────
    # Declarative metadata — read by the pipeline / serializer /
    # manifest loader to decide "which pass does this type belong to?"
    # ─────────────────────────────────────────────────────────────
    KIND: ClassVar[Literal["executable", "compound", "tool_source", "control_flow"]] = "executable"
    # pass-2 ordering integer (None ⇒ not compound). The serializer /
    # runtime sort compound types on this key. Legacy values
    # (preserved for back-compat): parallel=10, condition=20,
    # loop=30, router=40.
    COMPOUND_PASS: ClassVar[Optional[int]] = None
    # True for nodes that live in `ctx.tool_objects` and get wired
    # into agents by pass 3 (tools / http / mcp).
    IS_TOOL_SOURCE: ClassVar[bool] = False
    # True for nodes that should have their `tools=[...]` list
    # replaced by the pipeline with attached tool-source nodes
    # (currently only the agent type).
    NEEDS_TOOL_WIRING: ClassVar[bool] = False
    # `"agent"` | `"ask"` | `"none"`. Pass-1.5 wraps the
    # object in `Step(name=..., agent=...)` for the first two;
    # compound types are their own agno object and don't need a
    # Step wrapper.
    STEP_WRAPPER: ClassVar[Literal["agent", "ask", "none"]] = "none"

    # ─────────────────────────────────────────────────────────────
    # Behaviour hooks — subclasses override what they implement.
    # ─────────────────────────────────────────────────────────────
    def build(self, nid: str, node: dict, ctx: Any) -> Any:
        """Build the runtime object (Agent / Step / Router / ...).

        Called by `compile/pipeline.py` in the appropriate pass:
          - pass 0 (tool sources) only when `IS_TOOL_SOURCE` is True
          - pass 1 (top-level objects) for non-compound, non-tool types
          - pass 2 (compounds) when `COMPOUND_PASS` is not None

        Default raises so a subclass that forgets to implement it
        surfaces at the first workflow run, not silently.
        """
        raise NotImplementedError(
            f"{type(self).__name__}.build() is not implemented"
        )

    def to_source(self, nid: str, node: dict, ctx: Any) -> str:
        """Emit the Python source for this node.

        Called by `compile/serialize.py` in the matching pass.
        Returns an empty string when the node has no module-level
        emission (compound nodes handled by their own pass, tool-source
        wrappers handled by `build_tools` instead, etc.).

        Default raises so a missing override is loud.
        """
        raise NotImplementedError(
            f"{type(self).__name__}.to_source() is not implemented"
        )

    # ─────────────────────────────────────────────────────────────
    # Optional hook — only tool-source types implement this.
    # ─────────────────────────────────────────────────────────────
    def build_tools(
        self,
        nid: str,
        ir_node: Any,
        ir_nodes: dict,
        *,
        user_id: Optional[str] = None,
    ) -> list[Any]:
        """Build agno tool instances for a tool-source node.

        Called by `tool_factories.build_tools_for_node(...)` — that
        module is the public dispatcher that any consumer (the
        agent pass-3 wiring, the source export) reaches through.

        Default returns an empty list so a strategy that isn't a
        tool source doesn't have to implement this.
        """
        return []

__all__ = ["NodeStrategy"]