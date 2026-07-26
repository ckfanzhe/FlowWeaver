#!/usr/bin/env python3
"""Generate `frontend/src/types/node-configs.generated.ts` from the
Pydantic schemas.

The per-node TypeScript config interfaces used to live in
`frontend/src/types/workflow.ts:99-330` as a hand mirror of
`backend/src/app/schemas/node_configs.py`. Drift was easy —
adding a new field on the Python side required an unrelated edit
on the TS side, and typecheck didn't always catch the missed mirror.

This codegen pass walks the Pydantic `model_fields` and renders TS
interfaces. Both sides now evolve together: add a field on Python,
re-run this script, the TS picks it up. The companion CI check
(`scripts/check_node_configs_consistency.py`) re-runs the generator
and compares bytes against the on-disk file to catch forgotten regens.

Field key policy: Pydantic aliases (camelCase) when present, else the
Python name. The TS keys match the JSON wire format the frontend
already speaks — `modelId`, `toolsRef`, `requiresConfirmation`, etc.

Optional policy: only `Optional[T]` / `T | None` fields get `?` in the
TS. Fields with a default but a bare type (`str`, `bool`, `int`) stay
required — Pydantic always fills in the default, so the field is
always present at runtime, and consumers can rely on `.length` /
`.toString()` without null-checks.

Output shape (sorted consistently with the existing
`generate_node_types.py` + `generate_connection_rules_ts.py`
scripts):

    // ─── DO NOT EDIT — regenerate with scripts/generate_node_configs_ts.py
    export interface ModelConfig { ... }
    ...
    export interface HumanInputNodeConfig { ... }
    export type NodeConfig =
      | AgentNodeConfig
      | McpNodeConfig
      | ...;

Run:
    python scripts/generate_node_configs_ts.py

Companion CI check:
    python scripts/check_node_configs_consistency.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from pydantic import BaseModel  # noqa: E402

from app.schemas.node_configs import (  # noqa: E402
    AgentNodeConfig,
    BranchNodeConfig,
    BranchTarget,
    ConditionEvaluator,
    FlowNodeConfig,
    AskConfig,
    KnowledgeNodeConfig,
    KnowledgeSource,
    LoopNodeConfig,
    ModelConfig,
    ParamSchema,
    RouterSelector,
    ToolFunction,
    ToolNodeConfig,
)

OUT_PATH = REPO_ROOT / "frontend" / "src" / "types" / "node-configs.generated.ts"

# Order matters — nested types first so TS sees the dependencies.
# Mirrored by `UNION_MEMBERS` so a type appears once in the file.
# Node-type collapses (mode-discriminated):
#   - flow: parallel | sequential primitive (was parallel + steps)
#   - branch: switch | if-else primitive (was router + condition)
#   - tool: http | mcp | function source (was http + mcp + tools)
#   - knowledge: lancedb | pgvector | chroma vectorDb + openai |
#     sentence_transformers | cohere embedder — see
#     [[gleaming-munching-grove]].
SCHEMAS_IN_ORDER: list[type[BaseModel]] = [
    ModelConfig,
    ParamSchema,
    ToolFunction,
    BranchTarget,
    RouterSelector,
    ConditionEvaluator,
    AgentNodeConfig,
    ToolNodeConfig,
    BranchNodeConfig,
    FlowNodeConfig,
    LoopNodeConfig,
    AskConfig,
    KnowledgeSource,
    KnowledgeNodeConfig,
]

# The discriminated union that consumers narrow on via `node.type`.
UNION_MEMBERS: list[type[BaseModel]] = [
    AgentNodeConfig,
    ToolNodeConfig,
    BranchNodeConfig,
    FlowNodeConfig,
    LoopNodeConfig,
    AskConfig,
    KnowledgeNodeConfig,
]


def _is_union(origin: object) -> bool:
    """True if `origin` is `typing.Union` OR `types.UnionType` (the
    PEP 604 form `X | Y` returns the latter from `get_origin`)."""
    return origin is Union or (
        hasattr(types, "UnionType") and origin is types.UnionType
    )


def _is_optional(annotation: object) -> bool:
    """True if `annotation` is `Optional[X]` or `X | None`."""
    if _is_union(get_origin(annotation)):
        return type(None) in get_args(annotation)
    return False


def _python_to_ts(annotation: object) -> str:
    """Render a single Python annotation as a TS type string.

    Supported shapes: `str`/`int`/`float`/`bool`, `list[T]`,
    `dict[str, T]`, `Literal[a, b, c]`, `Optional[T]` / `T | None`,
    `Union[A, B]`, nested `BaseModel` subclasses, and `typing.Any`.
    """
    if annotation is Any:
        return "unknown"

    origin = get_origin(annotation)
    args = get_args(annotation)

    # Literal[a, b, c] → 'a' | 'b' | 'c'
    if origin is Literal:
        return " | ".join(repr(a) for a in args)

    # list[T] → T[]
    if origin is list:
        return f"{_python_to_ts(args[0])}[]"

    # dict[str, T] → Record<string, T>
    if origin is dict:
        key_type, value_type = args
        if key_type is not str:
            raise TypeError(
                f"unsupported dict key type: {key_type!r} (only dict[str, T] is mapped)"
            )
        return f"Record<string, {_python_to_ts(value_type)}>"

    # Union / Optional / PEP 604 `X | Y`
    if _is_union(origin):
        non_none = [a for a in args if a is not type(None)]
        # Optional[T] — single non-None member → `T | null`
        if len(non_none) == 1:
            return f"{_python_to_ts(non_none[0])} | null"
        return " | ".join(_python_to_ts(a) for a in args)

    # Nested BaseModel subclass → interface name
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation.__name__

    # Bare primitives
    if annotation is str:
        return "string"
    if annotation is int:
        return "number"
    if annotation is float:
        return "number"
    if annotation is bool:
        return "boolean"
    if annotation is type(None):
        return "null"

    raise TypeError(f"unsupported annotation: {annotation!r}")


def _render_interface(model: type[BaseModel]) -> str:
    """Render one Pydantic class as a TS interface.

    Keys come from `info.alias` when set, otherwise the Python
    attribute name. The `?` marker is only emitted for `Optional[T]`
    fields — defaults on bare-type fields are always filled in by
    Pydantic at runtime, so the field is always present.

    Annotations are resolved via `typing.get_type_hints()` so
    forward references (e.g. a top-level `ToolNodeConfig` whose
    `functions: list[ToolFunction]` references a class declared
    later in the same module) get evaluated to concrete types
    instead of staying as `ForwardRef('list[ToolFunction]')`.
    Without this, the new flat `ToolNodeConfig` (declared before
    `ParamSchema` / `ToolFunction`) crashes the generator.
    """
    lines = [f"export interface {model.__name__} {{"]
    # Resolve forward refs via `get_type_hints` so cross-class refs work
    # even when the class is declared before its referenced type.
    # `include_extras=False` keeps `Annotated[...]` unwrapped.
    resolved = get_type_hints(model, include_extras=False)
    for _name, info in model.model_fields.items():
        key = info.alias or _name
        annotation = resolved.get(_name, info.annotation)
        ts_type = _python_to_ts(annotation)
        optional = _is_optional(annotation)
        marker = "?" if optional else ""
        lines.append(f"  {key}{marker}: {ts_type};")
    lines.append("}")
    return "\n".join(lines)


def generate() -> str:
    """Return the full file body for the generated TS module."""
    parts: list[str] = [
        "/**",
        " * GENERATED FILE — DO NOT EDIT.",
        " *",
        " * Source of truth: backend/src/app/schemas/node_configs.py",
        " * Regenerate with:  python scripts/generate_node_configs_ts.py",
        " * CI check:         python scripts/check_node_configs_consistency.py",
        " *",
        " * The per-node TS interfaces used to live in",
        " * `workflow.ts:99-330` as a hand mirror of the Pydantic",
        " * schemas. Drift was easy. This file is now codegen — adding a",
        " * field on the Python side requires re-running this script and",
        " * the TS picks it up. Both sides evolve together.",
        " *",
        " * Field keys use the Pydantic alias (camelCase) when present so",
        " * the TS matches the JSON wire format the frontend already",
        " * speaks (`modelId`, `toolsRef`, `requiresConfirmation`, ...).",
        " *",
        " * `workflow.ts` re-exports these types so existing imports",
        " * (`import type { AgentNodeConfig } from '../../types/workflow'`)",
        " * keep working unchanged.",
        " */",
        "",
    ]
    for schema in SCHEMAS_IN_ORDER:
        parts.append(_render_interface(schema))
        parts.append("")

    parts.append("export type NodeConfig =")
    for i, member in enumerate(UNION_MEMBERS):
        prefix = "  | " if i > 0 else "    "
        parts.append(f"{prefix}{member.__name__}")
    parts.append(";")
    parts.append("")
    return "\n".join(parts)


def main() -> int:
    body = generate()
    # Atomic write — temp file + rename, so a crash mid-write doesn't
    # leave a half-written generated.ts on disk.
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(OUT_PATH.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(OUT_PATH)

    print(
        f"wrote {OUT_PATH.relative_to(REPO_ROOT)} "
        f"({len(SCHEMAS_IN_ORDER)} interfaces, "
        f"{len(UNION_MEMBERS)}-member union)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())