"""Node-type strategies (phase / .A, ).

A `NodeStrategy` is the per-type object that knows how to compile and
serialize one node kind. Every manifest entry's `runtime.module` +
`runtime.builder` resolves to a `NodeStrategy` subclass; the manifest
loader looks it up via `resolve_strategy(name)` and pins it onto
`spec.strategy` for the pipeline + serializer to consume.

Adding a brand-new node type:

    1. Drop a class deriving from `NodeStrategy` in `strategies/<name>.py`.
    2. Add a manifest entry whose `runtime.builder` names the class.
    3. (No edits to `pipeline.py` / `serialize.py` — they read
       `spec.strategy.build()` / `spec.strategy.to_source()` directly.)

Pattern mirrors Agno's `Toolkit` factories (`TavilyTools(Toolkit)`
wraps `Function.from_callable(...)` for the tool itself) and n8n's
`INodeTypeDescription` (a single declarative object per node type,
with `execute()` / `supplyData()` methods).
"""
from __future__ import annotations

import importlib
from typing import Any

from .base import NodeStrategy

__all__ = ["NodeStrategy", "resolve_strategy"]

def _instantiate_one(spec: Any) -> NodeStrategy:
    """Resolve one manifest entry's `runtime.builder` to a `NodeStrategy`.

    After phase every manifest row points at a `NodeStrategy` subclass
    (the emitter-module + callable shapes were retired in .A).
    Anything else is a manifest bug — the resolver raises with a
    specific name so the drift surfaces at registry build, not at the
    first workflow run.

    Detection rules (applied in order):

      1. `attr` IS a `NodeStrategy` instance → return as-is.
      2. `attr` IS a class deriving from `NodeStrategy` (but not
         `NodeStrategy` itself — the ABC, not a usable strategy)
         → instantiate with no args.
      3. Anything else → raise. The bridge used to wrap unknown shapes
         in `LegacyModuleStrategy` / `LegacyCallableStrategy`; those
         adapters are gone and we now fail loud.
    """
    attr: Any = None
    try:
        module = importlib.import_module(spec.runtime_module_path)
        attr = getattr(module, spec.runtime_builder_name, None)
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            f"node type {spec.name!r}: cannot import "
            f"{spec.runtime_module_path!r} — {exc}"
        ) from exc
    if attr is None:
        raise RuntimeError(
            f"node type {spec.name!r}: {spec.runtime_module_path}."
            f"{spec.runtime_builder_name} does not exist (manifest drift?)"
        )
    if isinstance(attr, NodeStrategy):
        return attr
    if (
        isinstance(attr, type)
        and issubclass(attr, NodeStrategy)
        and attr is not NodeStrategy
    ):
        return attr()
    raise RuntimeError(
        f"node type {spec.name!r}: {spec.runtime_module_path}."
        f"{spec.runtime_builder_name} resolved to {type(attr).__name__}; "
        "expected a NodeStrategy subclass"
    )

def resolve_strategy(name: str) -> NodeStrategy:
    """Return the `NodeStrategy` instance for one node type.

    Reads from `NODE_TYPES[name].strategy`, which `node_types._build_registry()`
    populates once per process (via `_instantiate_one` directly, with
    preset→parent instance sharing for entries that re-declare the
    parent's builder verbatim). The first `resolve_strategy` call
    triggers `_build_registry` if it hasn't run yet; subsequent calls
    return the cached instance.
    """
    from app.core.node_types import NODE_TYPES

    if name not in NODE_TYPES:
        raise KeyError(
            f"no strategy for node type {name!r}; known: {sorted(NODE_TYPES)}"
        )
    return NODE_TYPES[name].strategy