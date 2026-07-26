"""Manifest-driven node type registry.

The single source of truth for declarative node-type metadata is
`shared/nodes.manifest.json` (lives at the repo root, sibling to
`shared/connection_rules.json`). This module:

  1. Loads the manifest JSON.
  2. Validates it against `NodeManifestSpec` (Pydantic) — fail-loud at
     startup on malformed JSON, missing required fields, or broken
     module/class references.
  3. Resolves string keys to live Python objects (Pydantic classes,
     emitter modules' functions) via `importlib` + `getattr`.
  4. Publishes `NODE_TYPES: dict[str, NodeTypeSpec]` — the registry
     every consumer reads from.

Stage 1 scope (this commit): the loader is READ-ONLY. Consumers in
`schemas/node_configs.py`, `schemas/workflow.py`, `generator/emitters/`,
and `main.py` are migrated to read from this registry. Runtime
dispatch (`workflow_builder._node_to_step`, `legacy_bridge` if/elif)
and generator pipeline dispatch (pass ordering in `pipeline.py` /
`assembly.py` / `tools_expr.py` / `http_wrappers.py` /
`emitters/parallel.py`) are NOT migrated here — they stay as
hard-coded if/elif until stage 2 / stage 3.

The manifest's `runtime.module` / `runtime.builder` strings are
metadata-only at this stage; stage 2 will turn them into the actual
runtime dispatch path.

Schema design references (see docs/RESEARCH_REPORT.md):
  - n8n's `INodeTypeDescription` (declarative metadata object)
  - RAGFlow's `component_class` dict + JSON Schema validation
  - Airflow's `_task_module` reference + `__version` field
"""
from __future__ import annotations

import functools
import importlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# ─────────────────────────────────────────────────────────────────
# Manifest file location
# ─────────────────────────────────────────────────────────────────
# Lives at the repo root (`shared/`), sibling to `connection_rules.json`.
# Both backend and frontend consume cross-stack configuration from here.
# Resolve from this file's location: backend/src/app/core/ → ... → repo.
_SHARED_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent.parent
    / "shared"
)
_MANIFEST_PATH = _SHARED_DIR / "nodes.manifest.json"

# ─────────────────────────────────────────────────────────────────
# Pydantic schemas — manifest JSON shape
# ─────────────────────────────────────────────────────────────────
# Validating against a Pydantic schema at load time catches typos,
# missing fields, and broken module references BEFORE the registry is
# consumed (RAGFlow's `param_validation/{ClassName}.json` pattern;
# Airflow's `schema.json` pattern). Bad JSON → fail-loud at startup,
# not silent corruption at first node.

class _EmitterSpec(BaseModel):
    """`emitter` block — Python module path + a couple flags. (v1 only.)

    Deprecated by `capabilities` in v2 — kept here so v1 manifests still
    parse. The two blocks are reconciled in `_v1_to_v2_entry()` below:
    `emitter.needsToolWiring` → `capabilities.needsToolWiring`,
    `emitter.pass2` → `capabilities.compoundPass is not None`,
    `emitter.pass2Order` → `capabilities.compoundPass`,
    `emitter.skipPass1` → `capabilities.skipPass1`.

    `module` is purely metadata at this stage (no consumer resolves it
    via importlib for source emission — `compile/emitters/` is hard-coded
    by type in `compile/pipeline.EMITTERS`).
    """
    model_config = ConfigDict(extra="ignore")
    module: str = ""
    needsToolWiring: bool = False
    pass2: bool = False
    skipPass1: bool = False
    pass2Order: int = 0

class _RuntimeSpec(BaseModel):
    """`runtime` block — module + builder function/class name.

    In v2, `builder` may name either a callable OR a class deriving from
    `NodeStrategy` (convention: callable when the name starts with `_`,
    class otherwise). The legacy `_runtime_builders()` resolver returns
    a bound method on the strategy instance; if the resolved attr is a
    class, it instantiates it. This keeps the existing emitter-function
    path working while opening the door to strategy subclasses.

    The prior `toolkitClass` / `toolkitMethods` fields were deleted —
    preset metadata now lives in
    `app.core.strategies.tool.PRESET_REGISTRY` (per-preset dataclass).
    """
    model_config = ConfigDict(extra="ignore")
    module: str
    builder: str

class _IoSpec(BaseModel):
    """`io` block — three-axis capability declaration
    (App Inventor `properties / methods / events` analog).

    `inputs`  = what the node consumes
    `outputs` = what the node produces
    `tools`   = side effects (LLM calls, file I/O, external requests)

    Stage 1: metadata-only, not consumed by any consumer.
    """
    model_config = ConfigDict(extra="ignore", frozen=True)
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)

# ─────────────────────────────────────────────────────────────────
# v2 additions — `kind` / `extends` / `overrides` / `capabilities` / `ui`
# ─────────────────────────────────────────────────────────────────
# These blocks were added to make the manifest the sole source of
# truth for node-type behavior. See `docs/node-types.md` for the
# rationale and the migration path.
class _CapabilitiesSpec(BaseModel):
    """Single declarative block replacing `emitter.{pass2,pass2Order,
    skipPass1,needsToolWiring}` and `step_wrapper`. Every field has a
    sensible default so a v2 entry only declares what differs.

    `compoundPass` = pass-2 ordering integer (None ⇒ not compound). The
                     pipeline sorts `NODE_TYPES.values()` on this key.
                     Legacy values: parallel=10, condition=20, loop=30,
                     router=40.
    `isToolSource` = the node's value lives in `ctx.tool_objects` and
                     is wired into agents by `_pass3_tool_wiring`.
    `isKnowledgeSource` = the node's value lives in `ctx.knowledge_objects`
                     and is wired into agents' `knowledge=...` by
                     `_pass3_knowledge_wiring`. New in
                     [[gleaming-munching-grove]].
    `needsToolWiring` = an Agent node that should have its `tools=[...]`
                        replaced with attached tool-source nodes in pass 3.
    `needsKnowledgeWiring` = an Agent node that should have its `knowledge=...`
                        replaced with attached knowledge-source nodes in
                        pass 3b. Always false in v1 — the runtime sets
                        `agent.knowledge = kb` post-build, and the
                        source-emission pass emits the wiring line.
    `skipPass1`    = compound types whose pass-1 emission is empty
                     (parallel, loop) skip pass 1 entirely so the
                     generated file's section headers stay clean.
    `stepWrapper`  = `"agent"` | `"ask"` | `"none"`. Pass-1.5
                     wraps the object in `Step(...)` only for these two
                     types; compound types are their own agno object.
                     `ask` replaces `human_input`.
    """
    model_config = ConfigDict(extra="ignore")
    compoundPass: Optional[int] = None
    isToolSource: bool = False
    isKnowledgeSource: bool = False
    needsToolWiring: bool = False
    needsKnowledgeWiring: bool = False
    skipPass1: bool = False
    stepWrapper: Literal["agent", "ask", "none"] = "none"

class _UiSpec(BaseModel):
    """Frontend breadcrumbs — group / form / palette order.

    `group`       = palette-group label (was `paletteGroup` in v1, kept
                    on the v1 entry as the source; v2 moves it under `ui`).
    `form`        = form-component name in
                    `frontend/src/components/PropertyPanel/forms/registry.ts`.
                    Preset types inherit their parent's form unless they
                    set this explicitly.
    `paletteOrder` = drives the palette bar ordering (was top-level in v1).
    """
    model_config = ConfigDict(extra="ignore")
    group: str = ""
    form: str = ""
    paletteOrder: int = 0

class _OverridesSpec(BaseModel):
    """Preset-only block — shallow-merged on top of the parent's
    resolved spec. Currently only `defaultConfig` is meaningfully
    overrideable; the rest of the entries are visual (icon / color /
    displayName) and live as direct fields on the preset entry."""
    model_config = ConfigDict(extra="ignore")
    defaultConfig: dict[str, Any] = Field(default_factory=dict)

class _NodeEntryV1(BaseModel):
    """v1 manifest entry — kept for backward compat. New fields are
    tolerated as extra so a v1 row can pass through with `extra="ignore"`
    on every nested model."""
    model_config = ConfigDict(extra="ignore")
    category: str
    displayName: str
    i18nKey: str
    color: str
    textColor: str
    icon: str
    paletteOrder: int = Field(ge=1)
    paletteGroup: Optional[str] = None
    configSchemaRef: str
    defaultConfig: dict[str, Any] = Field(default_factory=dict)
    emitter: _EmitterSpec = Field(default_factory=_EmitterSpec)
    runtime: _RuntimeSpec
    io: _IoSpec = Field(default_factory=_IoSpec)

class _NodeEntryV2(BaseModel):
    """v2 manifest entry — superset of v1. Additions: `kind`, `extends`,
    `overrides`, `capabilities`, `ui`.

    `kind` is the semantic role (`executable` | `compound` |
    `tool_source` | `knowledge_source` | `control_flow`) used by the
    strategy registry; `category` is the visual group label
    (Core/Search/Data/Connectors). `control_flow` was added for the
    `ask` node (formerly `executable`). `knowledge_source` was added
    for the `knowledge` node — RAG sources parallel to `tool_source`.

    `extends` enables preset inheritance: a preset's resolved spec is
    built by deep-merging the parent's resolved spec + the preset's own
    fields + `overrides`. `kind`, `capabilities`, `configSchemaRef`,
    `runtime`, `emitter`, `io` are NOT overridable per-preset — those
    are behavior, not metadata.
    """
    model_config = ConfigDict(extra="ignore")
    kind: Literal[
        "executable", "compound", "tool_source", "knowledge_source", "control_flow"
    ]
    extends: Optional[str] = None
    overrides: _OverridesSpec = Field(default_factory=_OverridesSpec)
    category: str = ""
    displayName: str = ""
    i18nKey: str = ""
    color: str = ""
    textColor: str = ""
    icon: str = ""
    paletteOrder: int = Field(default=0, ge=0)
    configSchemaRef: str = ""
    defaultConfig: dict[str, Any] = Field(default_factory=dict)
    capabilities: _CapabilitiesSpec = Field(default_factory=_CapabilitiesSpec)
    ui: _UiSpec = Field(default_factory=_UiSpec)
    emitter: _EmitterSpec = Field(default_factory=_EmitterSpec)
    runtime: _RuntimeSpec
    io: _IoSpec = Field(default_factory=_IoSpec)

class NodeManifestSpec(BaseModel):
    """Top-level shape of `shared/nodes.manifest.json`.

    `schemaVersion` (int) lets us evolve the manifest format later
    without breaking older JSON files (Airflow's `__version` pattern).
    The dual-parse in `_validated_manifest()` accepts v1 OR v2 and
    normalises both to v2 internally so consumers always see the same
    shape.
    """
    model_config = ConfigDict(extra="ignore")
    schemaVersion: int
    nodes: dict[str, Any]  # entry shape varies by version — parsed separately

# ─────────────────────────────────────────────────────────────────
# Runtime typed shape — what consumers see
# ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class NodeTypeSpec:
    """The resolved, typed view of one manifest entry.

    Produced by `_resolve_entry()` from a validated `_NodeEntryV2`
    (v1 entries are first normalised via `_v1_to_v2_entry()`).
    Frozen so downstream consumers can't accidentally mutate the
    registry (Blockly's `Object.create(null)` discipline: no shared
    mutable state). The `default_config` dict contents are still
    technically mutable in Python, but the field itself can't be
    reassigned — close enough for our purposes; we trust consumers
    not to mutate it.

    v2 additions: `kind`, `extends`, `capabilities`, `ui`, `strategy`.
    Legacy v1 fields (`emitter_*`, `runtime_*`) stay until the
    dispatch migration is complete.
    """
    name: str
    kind: str
    category: str
    display_name: str
    i18n_key: str
    color: str
    text_color: str
    icon: str
    palette_order: int
    # Visual group for the palette bar. When the manifest omits it
    # we default to a category-derived label ("Core" / "Connectors")
    # so the palette still renders a sensible grouping for the
    # original entries that don't set it explicitly.
    palette_group: str
    config_schema: type[BaseModel]
    default_config: dict[str, Any]
    # v2 fields
    extends: Optional[str]
    capabilities: _CapabilitiesSpec
    ui: _UiSpec
    # Legacy v1 fields (still read by the pipeline until the
    # dispatch migration is complete)
    emitter_module_path: str
    emitter_needs_tool_wiring: bool
    emitter_pass2: bool
    emitter_skip_pass1: bool
    emitter_pass2_order: int
    runtime_module_path: str
    runtime_builder_name: str
    io: _IoSpec
    # The prior `toolkit_class` / `toolkit_methods` fields were deleted
    # — preset metadata now lives in
    # `app.core.strategies.tool.PRESET_REGISTRY` (per-preset dataclass
    # keyed by preset name). The 5 preset aliases route through the
    # unified `ToolStrategy`, which consults the registry.
    # Filled in lazily by the strategy resolver; None until
    # `_resolve_strategies()` has run for the first time.
    strategy: Optional[Any] = None

# ─────────────────────────────────────────────────────────────────
# Manifest loader (lazy + cached)
# ─────────────────────────────────────────────────────────────────
@functools.lru_cache(maxsize=1)
def _load_manifest_path() -> dict[str, Any]:
    """Read + JSON-parse the manifest. Cached for process lifetime
    (the file is frozen at startup, matching `connection_rules.py`'s
    pattern)."""
    with _MANIFEST_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)

@functools.lru_cache(maxsize=1)
def _validated_manifest() -> NodeManifestSpec:
    """Validate the raw JSON against `NodeManifestSpec`. Cached.

    Accepts v1 OR v2 (controlled by `schemaVersion`). Both shapes are
    normalised to v2 internally so every downstream consumer sees a
    single, unified `NodeTypeSpec`. v1 entries are converted by
    `_v1_to_v2_entry()`; v2 entries pass through with their declared
    `kind` / `capabilities` / `ui`.

    If a v2 entry declares `extends`, the parent is resolved by
    `_resolve_entry()` (which walks the chain depth-first with cycle
    detection). v1 entries never have `extends` — that's a v2-only
    concept.
    """
    raw = _load_manifest_path()
    schema_version = int(raw.get("schemaVersion", 1))
    if schema_version == 1:
        # Validate the per-entry v1 shape directly so `_v1_to_v2_entry`
        # sees typed `_NodeEntryV1` instances rather than raw dicts.
        # The top-level `NodeManifestSpec` declares `nodes: dict[str, Any]`
        # so a v1 pass would otherwise return raw dicts.
        try:
            v1_top = {
                "schemaVersion": 1,
                "nodes": {
                    name: _NodeEntryV1.model_validate(entry)
                    for name, entry in raw.get("nodes", {}).items()
                },
            }
            v1 = NodeManifestSpec.model_validate(v1_top)
        except ValidationError as e:
            raise ValueError(
                f"manifest v1 at {_MANIFEST_PATH} failed validation:\n{e}"
            ) from e
        normalised_nodes = {
            name: _v1_to_v2_entry(name, entry)
            for name, entry in v1.nodes.items()
        }
        return NodeManifestSpec(
            schemaVersion=2,
            nodes=normalised_nodes,
        )
    if schema_version == 2:
        try:
            v2_top = {
                "schemaVersion": 2,
                "nodes": {
                    name: _NodeEntryV2.model_validate(entry)
                    for name, entry in raw.get("nodes", {}).items()
                },
            }
            return NodeManifestSpec.model_validate(v2_top)
        except ValidationError as e:
            raise ValueError(
                f"manifest v2 at {_MANIFEST_PATH} failed validation:\n{e}"
            ) from e
    raise ValueError(
        f"manifest at {_MANIFEST_PATH}: unsupported schemaVersion "
        f"{schema_version}; expected 1 or 2"
    )

# ─────────────────────────────────────────────────────────────────
# v1 → v2 normaliser
# ─────────────────────────────────────────────────────────────────
def _v1_to_v2_entry(name: str, entry: _NodeEntryV1) -> _NodeEntryV2:
    """Convert a parsed v1 entry to its v2 equivalent.

    Mapping rules (mirrored in `docs/node-types.md`):
      `category` `executable` → `kind="executable"`
      `category` `tool_source` → `kind="tool_source"`
      `category` anything else → `kind="executable"` (forward-compat default)
      `emitter.pass2`/`pass2Order` → `capabilities.compoundPass`
      `emitter.needsToolWiring` → `capabilities.needsToolWiring`
      `emitter.skipPass1` → `capabilities.skipPass1`
      `step_wrapper` is inferred from `kind` + `pass2Order`:
        agent (non-compound, non-tool-source) → `stepWrapper="agent"`
        ask (same shape) → `stepWrapper="ask"`
        everything else → `stepWrapper="none"`
      `paletteGroup` (top-level on v1) → `ui.group`
      `paletteOrder` → `ui.paletteOrder`
      `ui.form` is left empty — the frontend registry falls back to
      the manifest type name (the form will be wired explicitly later).

    Behaviour changes for legacy entries (only `agent`/`ask`):
    these need a real `Step` wrapper at runtime, so `stepWrapper` is
    set explicitly. Other types default to `"none"`.
    """
    kind = entry.category if entry.category in {"executable", "compound", "tool_source", "knowledge_source", "control_flow"} else "executable"
    capabilities = _CapabilitiesSpec(
        compoundPass=entry.emitter.pass2Order if entry.emitter.pass2 else None,
        isToolSource=(kind == "tool_source"),
        isKnowledgeSource=(kind == "knowledge_source"),
        needsToolWiring=entry.emitter.needsToolWiring,
        skipPass1=entry.emitter.skipPass1,
        stepWrapper=(
            "agent" if name == "agent"
            else "ask" if name == "ask"
            else "none"
        ),
    )
    ui = _UiSpec(
        group=entry.paletteGroup or ("Core" if kind == "executable" else "Connectors"),
        form="",  # filled in by the form registry on first lookup
        paletteOrder=entry.paletteOrder,
    )
    return _NodeEntryV2(
        kind=kind,                    # type: ignore[arg-type]
        extends=None,
        overrides=_OverridesSpec(),
        category=entry.paletteGroup or ("Core" if kind == "executable" else "Connectors"),
        displayName=entry.displayName,
        i18nKey=entry.i18nKey,
        color=entry.color,
        textColor=entry.textColor,
        icon=entry.icon,
        paletteOrder=entry.paletteOrder,
        configSchemaRef=entry.configSchemaRef,
        defaultConfig=dict(entry.defaultConfig),
        capabilities=capabilities,
        ui=ui,
        emitter=entry.emitter,
        runtime=entry.runtime,
        io=entry.io,
    )

# ─────────────────────────────────────────────────────────────────
# Reference resolvers — string → live Python object
# ─────────────────────────────────────────────────────────────────
def _resolve_schema_class(ref: str) -> type[BaseModel]:
    """`configSchemaRef` → Pydantic class.

    The ref is the class NAME; we look it up in
    `app.schemas.node_configs` (which is the central registry for all
    per-type config schemas — same module `NODE_CONFIG_SCHEMA`
    already uses).
    """
    module = importlib.import_module("app.schemas.node_configs")
    cls = getattr(module, ref, None)
    if cls is None or not isinstance(cls, type) or not issubclass(cls, BaseModel):
        raise ValueError(
            f"manifest configSchemaRef {ref!r} did not resolve to a "
            "Pydantic BaseModel in app.schemas.node_configs"
        )
    return cls

def _resolve_entry(
    name: str,
    entry: _NodeEntryV2,
    *,
    _all_entries: dict[str, _NodeEntryV2] | None = None,
    _seen: Optional[set[str]] = None,
) -> NodeTypeSpec:
    """Build a `NodeTypeSpec` from a validated `_NodeEntryV2`.

    Schema resolution is eager — broken `configSchemaRef` surfaces at
    import time.

    Preset inheritance: when `entry.extends` is set, the parent's
    resolved spec is computed first and this preset's overrides are
    shallow-merged on top. Cycle detection via the `_seen` set raises
    at startup rather than at first node add. The parent must be
    present in `_all_entries`; missing parents are an error (we never
    silently inherit from "http" if it isn't declared).
    """
    _all_entries = _all_entries if _all_entries is not None else _validated_manifest().nodes
    _seen = _seen if _seen is not None else set()
    if name in _seen:
        raise ValueError(
            f"manifest: cycle in 'extends' chain at {name!r}: "
            f"already visited {sorted(_seen)}"
        )
    _seen = _seen | {name}

    if entry.extends is not None:
        if entry.extends not in _all_entries:
            raise ValueError(
                f"manifest: node type {name!r} extends unknown type "
                f"{entry.extends!r}"
            )
        parent_spec = _resolve_entry(
            entry.extends, _all_entries[entry.extends],
            _all_entries=_all_entries, _seen=_seen,
        )
        return _merge_preset_on_parent(name, entry, parent_spec)

    config_schema = _resolve_schema_class(entry.configSchemaRef)
    palette_group = entry.ui.group or _category_to_default_group(entry.category, entry.kind)
    return NodeTypeSpec(
        name=name,
        kind=entry.kind,
        category=entry.category or _kind_to_default_category(entry.kind),
        display_name=entry.displayName,
        i18n_key=entry.i18nKey,
        color=entry.color,
        text_color=entry.textColor,
        icon=entry.icon,
        palette_order=entry.ui.paletteOrder or entry.paletteOrder,
        palette_group=palette_group,
        config_schema=config_schema,
        default_config=dict(entry.defaultConfig),
        extends=entry.extends,
        capabilities=entry.capabilities,
        ui=entry.ui,
        emitter_module_path=entry.emitter.module,
        emitter_needs_tool_wiring=entry.capabilities.needsToolWiring,
        emitter_pass2=entry.capabilities.compoundPass is not None,
        emitter_skip_pass1=entry.capabilities.skipPass1,
        emitter_pass2_order=entry.capabilities.compoundPass or 0,
        runtime_module_path=entry.runtime.module,
        runtime_builder_name=entry.runtime.builder,
        io=entry.io,
        strategy=None,  # the strategy resolver fills this lazily
    )

def _merge_preset_on_parent(
    name: str,
    entry: _NodeEntryV2,
    parent: NodeTypeSpec,
) -> NodeTypeSpec:
    """Build a `NodeTypeSpec` for a preset by merging the parent's
    resolved spec with the preset's overrides.

    Overrideable: `displayName`, `color`, `textColor`, `icon`, `category`,
    `paletteGroup`, `paletteOrder`, `defaultConfig`. The preset's
    `kind` / `capabilities` / `configSchemaRef` / `emitter` / `io` are
    inherited verbatim from the parent — those are not overridable
    per-preset.

    `runtime` IS overridable: when the preset declares a different
    `module` / `builder` than its parent (the 5 preset aliases point
    at `ToolStrategy` while their `tool` parent also points at
    `ToolStrategy`), the preset gets its own strategy.
    When the preset's `runtime` happens to match the parent's (the
    legacy wikipedia preset extends http and re-declares http's
    `HttpToolStrategy`), the preset shares the parent's strategy
    INSTANCE so any ClassVar overrides applied to the parent propagate
    to every preset — see `_build_registry()` for the matching check.

    `palette_group` resolution mirrors the parent's logic when the
    preset's `ui.group` is empty.

    The prior `toolkit_class` / `toolkit_methods` fields were deleted
    — preset metadata now lives in
    `app.core.strategies.tool.PRESET_REGISTRY`. The wikipedia preset's
    HTTP defaults are now an entry in that registry; toolkit presets
    carry their `toolkit_class` + `toolkit_methods` there as well.
    """
    if entry.kind != parent.kind:
        # Crossing `kind` boundaries is rejected loudly — extending an
        # executable from a tool_source (or vice versa) is almost
        # certainly a typo in the preset entry.
        raise ValueError(
            f"manifest: preset {name!r} declares kind={entry.kind!r} "
            f"but its parent {parent.name!r} has kind={parent.kind!r}; "
            f"preset must inherit the parent's kind"
        )

    palette_group = entry.ui.group or parent.palette_group
    # `palette_order` distinguishes "not set" (None → use parent's)
    # from "set explicitly" (0 → suppress this preset from the palette
    # entirely). The previous `or`-chain collapsed 0 into parent, so
    # a preset couldn't opt out of the palette. The string fields
    # above (`color`, `icon`, …) still use `or` because an empty
    # string in those positions is always
    # an error, never an intentional value.
    preset_palette_order = (
        entry.ui.paletteOrder
        if entry.ui.paletteOrder is not None
        else entry.paletteOrder
    )
    return NodeTypeSpec(
        name=name,
        kind=parent.kind,
        category=entry.category or parent.category,
        display_name=entry.displayName or parent.display_name,
        i18n_key=entry.i18nKey or parent.i18n_key,
        color=entry.color or parent.color,
        text_color=entry.textColor or parent.text_color,
        icon=entry.icon or parent.icon,
        palette_order=(
            preset_palette_order
            if preset_palette_order is not None
            else parent.palette_order
        ),
        palette_group=palette_group,
        config_schema=parent.config_schema,
        default_config={
            **parent.default_config,
            **dict(entry.overrides.defaultConfig),
            **dict(entry.defaultConfig),  # legacy top-level defaultConfig overrides
        },
        extends=parent.name,
        capabilities=parent.capabilities,
        ui=_UiSpec(
            group=palette_group,
            form=entry.ui.form or parent.ui.form,
            paletteOrder=(
                preset_palette_order
                if preset_palette_order is not None
                else parent.ui.paletteOrder
            ),
        ),
        emitter_module_path=parent.emitter_module_path,
        emitter_needs_tool_wiring=parent.emitter_needs_tool_wiring,
        emitter_pass2=parent.emitter_pass2,
        emitter_skip_pass1=parent.emitter_skip_pass1,
        emitter_pass2_order=parent.emitter_pass2_order,
        # P2 : the preset's own `runtime.module` / `runtime.builder`
        # win over the parent's. Wikipedia re-declares the parent tool's
        # `ToolStrategy` (so it ends up sharing the parent's strategy
        # instance — see `_build_registry`). :
        # toolkit presets also share `ToolStrategy`; their per-preset
        # metadata now lives in `app.core.strategies.tool.PRESET_REGISTRY`
        # rather than on the manifest's `runtime` block.
        runtime_module_path=entry.runtime.module,
        runtime_builder_name=entry.runtime.builder,
        io=parent.io,
        strategy=None,  # `_build_registry` populates this in the post-process
    )

def _category_to_default_group(category: str, kind: str) -> str:
    """Visual palette-group default when neither `ui.group` nor
    `paletteGroup` is set. Mirrors the v1 fallback so existing manifests
    keep their two-group layout until they opt in to the v2 fields.

    `ask` (kind=`control_flow`) also lands in "Core" — same as
    agent, the gate sits next to the executor in the palette.
    """
    if category:
        return category
    if kind in ("executable", "control_flow"):
        return "Core"
    return "Connectors"

def _kind_to_default_category(kind: str) -> str:
    """Default visual `category` when neither `category` nor `ui.group`
    is set. Same defaults as `_category_to_default_group`."""
    if kind in ("executable", "control_flow"):
        return "Core"
    return "Connectors"

# ─────────────────────────────────────────────────────────────────
# Public registry
# ─────────────────────────────────────────────────────────────────
@functools.lru_cache(maxsize=1)
def _build_registry() -> tuple[NodeTypeSpec, ...]:
    """Walk the manifest, resolve every entry (preset inheritance
    included), and instantiate each entry's `NodeStrategy`. Cached
    for process lifetime.

    Each entry's `runtime.module` + `runtime.builder` resolves to a
    `NodeStrategy` subclass via `strategies._instantiate_one`. Preset
    entries that re-declare their parent's builder verbatim (the
    wikipedia case) share the parent's strategy instance so any
    ClassVar override on the parent propagates; tool presets that own
    their own builder get their own instance.

    Note: this function does NOT go through `strategies.resolve_strategy`
    — the resolver reads `_build_registry()` via `lru_cache`, so a
    direct call from here would deadlock on the first invocation.
    `_instantiate_one` is the same code path without the round-trip.
    """
    from app.core.strategies import _instantiate_one

    manifest = _validated_manifest()
    specs = [
        _resolve_entry(name, entry, _all_entries=manifest.nodes)
        for name, entry in manifest.nodes.items()
    ]
    # Per-`(runtime_module_path, runtime_builder_name)` cache so the
    # wikipedia preset (which re-declares http's builder) shares the
    # parent's strategy instance.
    strategies: dict[str, NodeStrategy] = {}
    specs_by_name = {s.name: s for s in specs}
    for spec in specs:
        if spec.extends and spec.extends in specs_by_name:
            parent_spec = specs_by_name[spec.extends]
            # Preset shares the parent's strategy INSTANCE only when
            # its `runtime.module` / `runtime.builder` exactly match
            # the parent's — that's the legacy wikipedia case (it
            # re-declares the parent `tool`'s ToolStrategy verbatim
            # so any ClassVar override on the parent propagates).
            # All 5 preset aliases route through the same `ToolStrategy`
            # as their `tool` parent, so the "fresh instance" branch is
            # no longer reachable for preset extensions — every preset
            # now shares the parent instance and the per-preset toolkit
            # class / methods are resolved at runtime via
            # `PRESET_REGISTRY`.
            if (
                spec.runtime_module_path == parent_spec.runtime_module_path
                and spec.runtime_builder_name == parent_spec.runtime_builder_name
            ):
                if spec.extends not in strategies:
                    strategies[spec.extends] = _instantiate_one(parent_spec)
                strategy = strategies[spec.extends]
            else:
                if spec.name not in strategies:
                    strategies[spec.name] = _instantiate_one(spec)
                strategy = strategies[spec.name]
        else:
            if spec.name not in strategies:
                strategies[spec.name] = _instantiate_one(spec)
            strategy = strategies[spec.name]
        object.__setattr__(spec, "strategy", strategy)
    return tuple(specs)

# Module-level singleton — every consumer reads from this.
NODE_TYPES: dict[str, NodeTypeSpec] = {
    spec.name: spec for spec in _build_registry()
}

# Convenience: ordered list of node names by `paletteOrder`. Used by the
# frontend (codegen will emit) and by the `/api/v1/node-types` endpoint.
PALETTE_ORDER: tuple[str, ...] = tuple(
    spec.name for spec in sorted(_build_registry(), key=lambda s: s.palette_order)
)

# ─────────────────────────────────────────────────────────────────
# node_config_schema — manifest-derived mapping for validation
# ─────────────────────────────────────────────────────────────────
# The schema registry lives here so `app.schemas.node_configs` can
# resolve a Pydantic class for any node type via the manifest. We
# keep `NODE_CONFIG_SCHEMA` (the legacy literal in that module) as a
# back-compat shim — see `node_configs.NODE_CONFIG_SCHEMA`.
@functools.lru_cache(maxsize=1)
def node_config_schema() -> dict[str, type[BaseModel]]:
    """Snapshot the manifest's `configSchemaRef` → Pydantic-class map.

    Built once per process (the manifest is immutable after startup,
    and so are the schema classes). Cached so per-request validation
    doesn't re-walk the registry.
    """
    return {name: spec.config_schema for name, spec in NODE_TYPES.items()}

# ─────────────────────────────────────────────────────────────────
# resolve_runtime_builder — manifest-driven runtime dispatch
# ─────────────────────────────────────────────────────────────────
# Each manifest entry declares where the runtime-compilation function
# lives (`runtime.module` / `runtime.builder`). The workflow_builder
# used to branch on `node.type` with an if/elif chain — now it calls
# the resolved callable. The signatures are NOT uniform across types
# (compound types take `(node, node_map, outgoing)`; `human_input`
# takes `(node, *, name, skip_pause)`; `legacy_bridge` types take
# `(node, *, name)`), so the dispatcher in `workflow_builder` handles
# those shape differences inline.
@functools.lru_cache(maxsize=1)
def _runtime_builders() -> dict[str, Callable]:
    """Resolve every entry's `runtime.builder` to a live callable."""
    out: dict[str, Callable] = {}
    for name, spec in NODE_TYPES.items():
        module = importlib.import_module(spec.runtime_module_path)
        fn = getattr(module, spec.runtime_builder_name, None)
        if not callable(fn):
            raise RuntimeError(
                f"runtime builder {spec.runtime_module_path}."
                f"{spec.runtime_builder_name} for node type {name!r} "
                "is not callable"
            )
        out[name] = fn
    return out

def resolve_runtime_builder(name: str) -> Callable:
    """Return the runtime-compilation callable for one node type.

    Used by `workflow_builder._node_to_step` and any
    other consumer that needs to compile a node into an agno Step /
    Router / Parallel / Condition / Loop.
    """
    table = _runtime_builders()
    if name not in table:
        raise KeyError(
            f"no runtime builder for node type {name!r}; known: {sorted(table)}"
        )
    return table[name]

__all__ = [
    "NodeTypeSpec",
    "NodeManifestSpec",
    "NODE_TYPES",
    "PALETTE_ORDER",
    "node_config_schema",
    "resolve_runtime_builder",
    # v2 schema blocks (P3, )
    "_CapabilitiesSpec",
    "_UiSpec",
    "_OverridesSpec",
    "_NodeEntryV1",
    "_NodeEntryV2",
    "_v1_to_v2_entry",
    "_resolve_schema_class",
]