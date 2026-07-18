"""Per-node-type Pydantic schemas — strict validation of `data.config`.

Earlier in development, `WorkflowNode.data.config` was `dict[str, Any]`
— a backwards-compatible hole that swallowed typos, missing fields,
and type mismatches until runtime (or worse, code export). This
module gives every node type a real schema so:

  - `POST /workflows` returns 422 with a clear Pydantic error when the
    client sends a missing `instructions`, a `condition` field that
    isn't a valid template, or a `loop.maxIterations` of `9999`.
  - The frontend's `NodeConfig` union gets a backend mirror, so any
    property-panel edit is structurally validated before save.
  - The runtime doesn't have to defend against bad data — the gate is
    at the API boundary.

Tolerance:
  - `extra="ignore"` so unknown fields (forward-compat for new fields
    the schema hasn't been updated for yet) are silently dropped.
  - Optional fields with sensible defaults so an empty `data.config`
    still parses (the seed shape from `workflowStore.ts`).

The mapping from `node.type` → schema is one-to-one and lives in
`NODE_CONFIG_SCHEMA` (derived from `shared/nodes.manifest.json`
via `app.core.node_types.node_config_schema()` — the literal
dict below is kept for Pydantic discriminated-union construction
only). `WorkflowNode` uses it via a model_validator so the API
layer doesn't have to dispatch manually.
"""
from __future__ import annotations

import sys
from typing import Annotated, Any, Iterable, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ─────────────────────────────────────────────────────────────────
# Common shape
# ─────────────────────────────────────────────────────────────────
# All configs share permissive settings — extra fields are ignored
# (forward-compat) and assignment by alias is allowed (the frontend
# sends camelCase). Pydantic's `model_config` is per-model; applying
# `extra="ignore"` here keeps every sub-schema consistent.
_BASE_CONFIG = ConfigDict(extra="ignore", populate_by_name=True)

# WRITE-time strict mirror. Used by the LLM tool surface ONLY
# (chat-builder add_node / update_node / plan_workflow). Ratified
# invariant: `test_extra_fields_silently_ignored` MUST stay green,
# so READ-time (workflow load, import_json, posted workflow save)
# continues to use the lax `_BASE_CONFIG` siblings. The strict
# class is generated on demand as a parallel sibling via
# `get_strict_schema()`.
_LLM_WRITE_CONFIG = ConfigDict(extra="forbid", populate_by_name=True)

# ─────────────────────────────────────────────────────────────────
# Per-type configs
# ─────────────────────────────────────────────────────────────────
class ModelConfig(BaseModel):
    """Agent's LLM choice — either via preset id OR inline."""
    model_config = _BASE_CONFIG

    provider: str = Field(
        default="openai",
        description=(
            "Provider name (`'openai'` / `'anthropic'` / `'ollama'` / `'google'`). "
            "Defaults to `'openai'`. Other strings are accepted as forward-compat "
            "for new providers that haven't been enumerated yet."
        ),
    )
    model_id: str = Field(
        default="",
        alias="modelId",
        description=(
            "Specific model id within the provider — e.g. `'gpt-4o'` or "
            "`'claude-sonnet-4-5'`. Empty string means use the preset's default."
        ),
    )
    api_key: Optional[str] = Field(
        default=None,
        alias="apiKey",
        description=(
            "Legacy per-node API key. Prefer `presetId` — the platform keeps "
            "API keys server-side per user, never in the workflow JSON."
        ),
    )
    base_url: Optional[str] = Field(
        default=None,
        alias="baseUrl",
        description=(
            "Custom API base URL. Leave empty for the provider default. Useful "
            "for self-hosted / proxy endpoints."
        ),
    )
    preset_id: Optional[str] = Field(
        default=None,
        alias="presetId",
        description=(
            "Reference to a saved `LlmPreset` (Settings → LLM Models). When "
            "set, the API key + baseUrl + provider come from the preset. "
            "`null` means use the user's default preset at runtime."
        ),
    )

class AgentNodeConfig(BaseModel):
    """An Agent node's editable fields.

    `model` is OPTIONAL — omitting it lets the runtime fall back to
    the user's chosen default LLM preset (Settings → LLM Models). The
    frontend's `workflowStore.createNew` ships a seed agent with no
    model so the default preset takes over. Seeding an explicit
    (provider, modelId) pair here used to short-circuit that fallback
    in `_agent_handler`; the seed workflow intentionally leaves
    `model` unset so the system default takes over.

    `instructions` defaults to an empty string so the canvas can
    render the seed workflow without a 422.

    Beyond the seed fields, 11 additional fields map 1:1 to agno's
    `Agent.__init__` high-frequency parameters. They're all optional
    with sensible defaults so existing workflows stay valid:

      - reasoning / reasoning_model — chain-of-thought auxiliary model
      - retries / delay_between_retries — tool-call retry policy
      - tool_call_limit — cap on tool calls per turn
      - add_datetime_to_context — inject `current_date` into the prompt
      - parser_model / parser_model_prompt — structured-output reparser
      - system_message — separate from `instructions` (developer-tier)
      - pre_hooks / post_hooks — callables run before/after the agent
        (must reference `tools` node IDs; see Plan )

    Bound ranges: `retries` 0..10, `delay_between_retries` 0..60,
    `tool_call_limit` 1..1000 (None = unlimited).
    """
    model_config = _BASE_CONFIG

    model: ModelConfig | None = Field(
        default=None,
        description=(
            "LLM choice for this agent. `null` lets the runtime fall back to "
            "the user's default preset (Settings → LLM Models). Seed agents "
            "leave this unset so the system default takes over."
        ),
    )
    instructions: str = Field(
        default="",
        description=(
            "Main agent instructions (the user-facing system prompt). Empty "
            "string is allowed — the runtime will render an empty `instructions` "
            "agent that just relays input to the model verbatim."
        ),
    )
    tools_ref: list[str] = Field(
        default_factory=list,
        alias="toolsRef",
        description=(
            "DEPRECATED — list of tool node IDs (mcp/http/tools) attached to "
            "this agent. Use `kind: 'tool_attachment'` edges on the canvas "
            "instead: wire the tool source node → agent node and the platform "
            "binds it. `toolsRef` is still read on load for back-compat."
        ),
    )
    markdown: bool = Field(
        default=True,
        description=(
            "Render agent output as Markdown in the chat transcript. "
            "`false` shows raw text."
        ),
    )
    requires_confirmation: list[str] = Field(
        default_factory=list,
        alias="requiresConfirmation",
        description=(
            "Tool names that require user confirmation before invocation (HITL). "
            "Empty list = auto-run every tool. Tool names match the manifest's "
            "toolkit methods or user-written function names."
        ),
    )

    # ──── Agent extended fields (the 11 high-frequency knobs) ─────────
    system_message: str = Field(
        default="",
        alias="systemMessage",
        description=(
            "Developer-tier system prompt — separate from `instructions` "
            "(which is user-facing). Used to inject platform-level guidance "
            "that shouldn't be exposed to the end-user."
        ),
    )
    reasoning: bool = Field(
        default=False,
        description=(
            "Enable chain-of-thought reasoning via an auxiliary model. The "
            "model is `reasoningModel` if set, otherwise the main `model`."
        ),
    )
    reasoning_model: Optional[ModelConfig] = Field(
        default=None,
        alias="reasoningModel",
        description=(
            "Optional auxiliary model for chain-of-thought reasoning. `null` "
            "= reuse the main `model`."
        ),
    )
    retries: int = Field(
        default=0,
        ge=0,
        le=10,
        description=(
            "Number of retries on tool-call failures. 0..10. `0` = no retry."
        ),
    )
    delay_between_retries: int = Field(
        default=1,
        ge=0,
        le=60,
        alias="delayBetweenRetries",
        description=(
            "Seconds to wait between retries. 0..60. Only applied when "
            "`retries > 0`."
        ),
    )
    tool_call_limit: Optional[int] = Field(
        default=None,
        ge=1,
        le=1000,
        alias="toolCallLimit",
        description=(
            "Cap on tool calls per agent turn. `null`/undefined = unlimited. "
            "1..1000. Useful for runaway-budget workflows."
        ),
    )
    num_history_runs: Optional[int] = Field(
        default=None,
        ge=1,
        le=50,
        alias="numHistoryRuns",
        description=(
            "/ session: number of prior runs to inject into the "
            "agent's prompt as history. `null` = the runtime's "
            "default (currently 5). 1..50. Higher values give the "
            "agent more conversational context at the cost of "
            "larger prompts and slower inference. Useful for long-"
            "running chats where the agent needs to remember tool "
            "calls from several turns back."
        ),
    )
    add_datetime_to_context: bool = Field(
        default=False,
        alias="addDatetimeToContext",
        description=(
            "Inject the current date into the prompt (`current_date` is "
            "appended to the context). Useful for time-sensitive workflows."
        ),
    )
    parser_model: Optional[ModelConfig] = Field(
        default=None,
        alias="parserModel",
        description=(
            "Optional model used to reparse structured output. `null` = the "
            "main agent output is taken verbatim."
        ),
    )
    parser_model_prompt: str = Field(
        default="",
        alias="parserModelPrompt",
        description=(
            "Prompt given to the parser model. Empty = agno's default "
            "reparse prompt."
        ),
    )
    # Hooks — list of `tools` node IDs. Dangling refs (deleted source)
    # are silently dropped at compile time. We don't validate at the
    # schema level because the IR may not be built yet on save.
    pre_hooks: list[str] = Field(
        default_factory=list,
        alias="preHooks",
        description=(
            "IDs of `tools` nodes whose functions run BEFORE the agent's main "
            "loop. Use this for pre-processing (load context, fetch state). "
            "Dangling refs (deleted source) are silently dropped."
        ),
    )
    post_hooks: list[str] = Field(
        default_factory=list,
        alias="postHooks",
        description=(
            "IDs of `tools` nodes whose functions run AFTER the agent's main "
            "loop. Use this for post-processing (log results, side-effects). "
            "Dangling refs are silently dropped."
        ),
    )

class ToolNodeConfig(BaseModel):
    """ — replaces the prior standalone
    `HttpNodeConfig` + `McpNodeConfig` + `ToolsNodeConfig`. The
    `source` discriminator (`'mcp'` | `'http'` | `'function'`)
    selects which tool-emit primitive to build at runtime.

     — collapses the prior 5 preset node types
    (`wikipedia` / `tavily_search` / `duckduckgo` / `calculator` /
    `arxiv_search`) into the `preset` discriminator on this single
    `tool` node. `preset` is checked BEFORE `source` by
    `ToolStrategy.build_tools` — HTTP preset (`wikipedia`) forces
    `source='http'` + merges preset defaults into cfg; toolkit
    presets (`tavily_search` / `duckduckgo` / `calculator` /
    `arxiv_search`) dispatch to `build_toolkit_for_preset(...)` via
    `app.core.strategies.tool.PRESET_REGISTRY`.

    Field groups (intentionally flat — matches F7 BranchNodeConfig
    pattern; runtime consumers read only fields relevant to `source`/
    `preset`):

      - preset (all variants may set): preset
      - shared (all sources may use): tool_name, tool_description
      - HTTP-only (source='http'): method, base_url, path, headers,
        query_params, body_schema, auth_token
      - MCP-only (source='mcp'): server_id, tool_name_prefix
      - function-only (source='function'): functions[], enabled_methods[],
        toolkit_options{}

    Default `source='function'` so a freshly-dropped `tool` node is a
    no-op empty-function source until the user picks a different mode.
    """
    model_config = _BASE_CONFIG

    # ── preset discriminator (, ) ─────────────
    # `null` = plain `tool` node (no preset). Set to one of the 5
    # preset names to prefill defaults + dispatch through PRESET_REGISTRY.
    # Checked BEFORE `source` by ToolStrategy — so a wikipedia preset
    # forces `source='http'` + applies HTTP defaults even if the
    # caller accidentally leaves `source='function'`.
    preset: Optional[Literal[
        "wikipedia", "tavily_search", "duckduckgo",
        "calculator", "arxiv_search",
    ]] = Field(
        default=None,
        description=(
            "Preset discriminator (, ). `null` = plain "
            "`tool` node; set to a preset name to prefill defaults + dispatch "
            "through `PRESET_REGISTRY` (`wikipedia` = HTTP wrapper against "
            "en.wikipedia.org; `tavily_search` / `duckduckgo` = web search "
            "toolkits; `calculator` = arithmetic toolkit; `arxiv_search` = "
            "arXiv preprint search toolkit)."
        ),
    )

    source: Literal["mcp", "http", "function"] = Field(
        default="function",
        description=(
            "Tool-source discriminator. `'http'` = HTTP wrapper "
            "(needs `base_url`); `'mcp'` = `MCPTools(...)` against a "
            "configured server (needs `server_id`); `'function'` = "
            "user-written Python functions via `functions[]`. "
            "Ignored when `preset` is set ()."
        ),
    )

    # ── shared (used by all sources; optional per source) ────────
    tool_name: str = Field(
        default="",
        alias="toolName",
        description=(
            "Tool name as seen by the attached agent. Empty = the agent sees "
            "an unnamed tool (under-emitting this field is a common mistake)."
        ),
    )
    tool_description: str = Field(
        default="",
        alias="toolDescription",
        description=(
            "Description the agent sees when deciding whether to invoke this "
            "tool. Empty = the agent has no signal about when to call it."
        ),
    )

    # ── HTTP-only (effective when source='http') ────────────────
    method: Literal["GET", "POST"] = Field(
        default="GET",
        description="HTTP method — `'GET'` or `'POST'`. Only used when `source='http'`.",
    )
    base_url: str = Field(
        default="",
        alias="baseUrl",
        description=(
            "Base URL concatenated with `path`. Only used when `source='http'`. "
            "Empty + POST returns a 422 at save time — the runtime can't "
            "build a valid request URL."
        ),
    )
    path: str = Field(
        default="",
        description=(
            "Path appended to `baseUrl` (e.g. `'/v1/current'`). Only used "
            "when `source='http'`."
        ),
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "HTTP headers as a `{name: value}` map. Only used when "
            "`source='http'`."
        ),
    )
    query_params: dict[str, str] = Field(
        default_factory=dict,
        alias="queryParams",
        description=(
            "Query parameters as a `{name: value}` map (GET only). Values "
            "are URL-encoded at request time. Only used when `source='http'`."
        ),
    )
    body_schema: str = Field(
        default="",
        alias="bodySchema",
        description=(
            "Optional JSON schema string describing the POST body shape. "
            "Only used when `source='http'`. Empty = no structured body."
        ),
    )
    auth_token: str = Field(
        default="",
        alias="authToken",
        description=(
            "Bearer token sent in the `Authorization` header. Only used "
            "when `source='http'`. Empty = no bearer auth."
        ),
    )

    # ── MCP-only (effective when source='mcp') ──────────────────
    server_id: str = Field(
        default="",
        alias="serverId",
        description=(
            "Reference to a configured MCP server (the `id` field of an "
            "`McpServerConfig` row in Settings → MCP). Empty string = "
            "no server wired. Wire to an agent with `kind='tool_attachment'`. "
            "Only used when `source='mcp'`."
        ),
    )
    tool_name_prefix: str = Field(
        default="",
        alias="toolNamePrefix",
        description=(
            "Prefix added to every tool name exposed by this MCP server. "
            "Only used when `source='mcp'`."
        ),
    )

    # ── function-only (effective when source='function') ────────
    functions: list[ToolFunction] = Field(
        default_factory=list,
        description=(
            "User-written Python functions exposed as tools. Each function's "
            "`code` is executed in a sandbox; the `parameters` schema is shown "
            "to the agent when filling calls. Only used when `source='function'`."
        ),
    )

    # Preset-toolkit fields. Used when `preset` is one of the 4
    # toolkits (`tavily_search` / `duckduckgo` / `calculator` /
    # `arxiv_search`). Plain `tool` nodes with `source='function'` or
    # HTTP preset (`wikipedia`) ignore them.
    enabled_methods: list[str] = Field(
        default_factory=list,
        description=(
            "Preset-toolkit field. Empty = expose ALL methods declared in "
            "`PRESET_REGISTRY[name].toolkit_methods`. Non-empty = intersect "
            "against the allowed list. Used when `preset` is a toolkit "
            "preset (tavily_search / duckduckgo / calculator / arxiv_search); "
            "plain `tool` nodes + wikipedia preset ignore it."
        ),
    )
    toolkit_options: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Preset-toolkit field: kwargs passed to the toolkit constructor. "
            "Used when `preset` is a toolkit preset; plain `tool` nodes + "
            "wikipedia preset ignore it."
        ),
    )

    # ── legacy `mode` → `source` migration (N4 compat shim) ─────
    # `_compat.migrate_node_dict` writes `cfg.mode` for every legacy
    # alias (`branch` writes `mode='switch'` / `'if-else'`, `flow`
    # writes `mode='parallel'` / `'sequential'`). For the N4 `tool`
    # aliases, the discriminator field is named `source`, not `mode`.
    # When a legacy envelope (`type:'http'|'mcp'|'tools'`) is read,
    # the `_compat` layer rewrites the type to `'tool'` and injects
    # `config.mode = 'http'|'mcp'|'function'`. This validator catches
    # that and copies `mode` into `source` (without overwriting an
    # explicit `source` if the caller already set one — preserves
    # forward-compatible behaviour).
    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_mode_to_source(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "source" in data:
            return data
        legacy_mode = (data.get("mode") or "").strip().lower()
        if legacy_mode in ("http", "mcp", "function"):
            data = dict(data)
            data["source"] = legacy_mode
        return data

class ParamSchema(BaseModel):
    model_config = _BASE_CONFIG

    name: str = Field(
        description=(
            "Parameter name as seen by the agent when filling the call. "
            "Required — no default."
        ),
    )
    type: Literal["string", "number", "boolean", "object"] = Field(
        default="string",
        description=(
            "Parameter type — `'string' | 'number' | 'boolean' | 'object'`. "
            "Defaults to `'string'`."
        ),
    )
    description: str = Field(
        default="",
        description=(
            "Description the agent sees when deciding what value to fill. "
            "Empty = the agent has no signal about the parameter's purpose."
        ),
    )
    required: bool = Field(
        default=False,
        description="Whether the parameter must be supplied by the agent.",
    )

class ToolFunction(BaseModel):
    model_config = _BASE_CONFIG

    name: str = Field(
        description=(
            "Function name as seen by the agent. Required — no default. "
            "Must be unique within the parent `tools` node's `functions[]`."
        ),
    )
    description: str = Field(
        default="",
        description=(
            "Description the agent sees when deciding whether to call this "
            "function. Empty = the agent has no signal about when to use it."
        ),
    )
    parameters: list[ParamSchema] = Field(
        default_factory=list,
        description=(
            "Parameter list — each entry is a `ParamSchema`. Order is "
            "preserved in the function signature."
        ),
    )
    code: str = Field(
        default="",
        description=(
            "Python source implementing the function body. The runtime "
            "executes it in a sandbox with the listed parameters bound. "
            "Empty = the function raises on call (no body)."
        ),
    )

class BranchTarget(BaseModel):
    model_config = _BASE_CONFIG

    label: str = Field(
        default="",
        description=(
            "Branch label — used as the canvas edge's `sourceHandle` and as "
            "the display text on the rendered edge. Empty = unlabeled "
            "(edge renders without a label)."
        ),
    )
    target: str = Field(
        default="",
        description=(
            "Target node id this branch wires to. Empty = branch is unwired "
            "(no edge to draw)."
        ),
    )
    # Per-branch selector hint (router only). Same syntax as the
    # condition node's `condition` field: `contains:foo`, `equals:bar`,
    # `always`, etc. — parsed at code-generation time by
    # `parse_condition_template`. Empty/None means "fall through to
    # the node-level default" (back-compat for parallel branches
    # which don't carry conditions).
    #
    # The per-branch `condition` field stays as-is — it still drives
    # the deterministic walk inside the LLM-fallback selector. The
    # rename is at the NODE level only.
    #
    # Default is now `None` instead of `""`. None is falsy exactly
    # like "", so consumers (`if branch.condition:`) see the same
    # behaviour. The change is purely cosmetic — it signals "this
    # field is unused" to readers. The router picker (which was the
    # only consumer) is gone. Both None and "" pass the same code
    # paths.
    condition: Optional[str] = Field(
        default=None,
        description=(
            "DEPRECATED per-branch selector hint. The router picker is "
            "gone — the runtime ignores this field. `null` and `''` are "
            "both valid and equivalent. Kept only for back-compat with "
            "legacy DSL strings (contains:/equals:/regex:/always)."
        ),
    )

class RouterSelector(BaseModel):
    """agno Router.selector — function / CEL / HITL (refined).

    Required field on every RouterNodeConfig. The three modes mirror
    agno's native `Router` capabilities that don't require an LLM call:

      - `function` — a Python expression evaluated against `step_input`.
        Same 5 locals as the Condition evaluator:
        `previous_step_content`, `previous_step_outputs`, `input`,
        `additional_data`, `session_state`. The expression must return
        the chosen branch step object (or a list of them).
      - `cel` — CEL expression string passed verbatim to agno.
        Returns the branch label (`<nid>_step`-bound name).
      - `hitl` — pause execution and ask the user to pick the branch
        (agno `Router(requires_user_input=True, user_input_message=...)`).
        No `selector` callable / CEL is emitted — agno handles it.

    `expression` is meaningful for `function` and `cel`. `fallback_message`
    is the prompt shown when `hitl` fires.

    Note: the earlier `mode='llm'` (defer to agno's LLM picker)
    was removed in an earlier release. Routing by LLM is the
    default in many workflow platforms but it introduces non-
    determinism + token cost; agno's recommended pattern is to write
    a deterministic selector (function / CEL) or defer to the user
    (HITL). Users who really want LLM picking can chain a Router
    with an Agent that returns the chosen branch label.
    """
    model_config = _BASE_CONFIG

    mode: Literal["function", "cel", "hitl"] = Field(
        default="function",
        description=(
            "Selector mode — `'function'` (Python expression evaluated against "
            "`step_input`), `'cel'` (CEL string passed to agno verbatim), or "
            "`'hitl'` (pause and ask the user to pick the branch). The router "
            "itself never makes an LLM call — routing is deterministic or "
            "human-driven."
        ),
    )
    expression: str = Field(
        default="",
        description=(
            "Dispatch expression. function mode: a Python expression wrapped as "
            "`return (<expr>)` — MUST return a branch step reference (e.g. "
            "`yes_agent_step if previous_step_content == 'yes' else no_agent_step`), "
            "NOT a label string. cel mode: a CEL expression string. hitl mode: "
            "unused (the user picks)."
        ),
    )
    fallback_message: str = Field(
        default="",
        alias="fallbackMessage",
        description=(
            "Prompt shown to the user when `mode='hitl'` fires (becomes "
            "`Router.user_input_message`). Empty = agno's built-in default "
            "prompt."
        ),
    )

class RouterNodeConfig(BaseModel):
    """ DEPRECATED — use `BranchNodeConfig`.

    Kept so `_compat.migrate_envelope` can still introspect legacy
    `type: "router"` envelopes on the read path; new workflows
    should never see this class.
    """
    model_config = _BASE_CONFIG
    selector: RouterSelector = Field(default_factory=lambda: RouterSelector(mode="function"))
    branches: list[BranchTarget] = Field(default_factory=list)

class FlowNodeConfig(BaseModel):
    """ — replaces the standalone `parallel` and
    `steps` nodes. `mode` selects between concurrent fan-out
    (`parallel`) and ordered pipeline (`sequential`); the
    `branches` list is wired via canvas edges (mirror of the IR).
    `requiresConfirmation` only takes effect in `sequential` mode —
    `loop` is the right primitive for per-iteration gating.
    """
    model_config = _BASE_CONFIG

    mode: Literal["parallel", "sequential"] = Field(
        default="parallel",
        description=(
            "`parallel` fans out branches concurrently. `sequential` runs "
            "branches in order, optionally gated by a single HITL "
            "confirmation (see `requiresConfirmation`)."
        ),
    )
    branches: list[BranchTarget] = Field(
        default_factory=list,
        description=(
            "Branch targets. Wired via canvas edges — this list is a "
            "mirror of the IR for display / older clients. In `parallel` "
            "mode the branches run concurrently; in `sequential` mode they "
            "run in edge order and the next step sees the previous step's "
            "output."
        ),
    )
    requires_confirmation: bool = Field(
        default=False,
        alias="requiresConfirmation",
        description=(
            "Block-level HITL — only effective in `sequential` mode. Pauses "
            "before the first step runs and asks the user to confirm. One "
            "prompt for the whole batch — use `loop` with "
            "`requiresIterationReview` for per-iteration confirmation."
        ),
    )
    confirmation_message: str = Field(
        default="",
        alias="confirmationMessage",
        description=(
            "Custom prompt shown when `requiresConfirmation` fires. Empty = "
            "agno's built-in default prompt."
        ),
    )

class ConditionEvaluator(BaseModel):
    """agno's `Condition.evaluator` — one of three modes.

    `function` mode: a Python expression evaluated against `step_input`.
      The runtime wraps it in a generated lambda exposing 5 locals:
        - `previous_step_content` (str | None) — content of the upstream step
        - `previous_step_outputs` (dict[str, str]) — all upstream outputs by name
        - `input` (str | None) — the workflow's input string
        - `additional_data` (dict[str, Any]) — workflow-level extra data
        - `session_state` (dict[str, Any]) — mutable session state
      The expression is wrapped as `return <expression>` inside a
      generated function — see `compile.condition.make_evaluator`.

    `cel` mode: a CEL expression string passed verbatim to agno
      (`Condition(evaluator="<cel_string>")`). Requires
      `cel-python` to be installed at runtime — see SPEC .

    `literal` mode: a boolean literal — the generated evaluator
      returns the value unchanged (`True` / `False`).

    Replaces the legacy `contains:/equals:/regex:` template DSL. The
    old `condition` field is still read for backward compatibility
    and auto-migrated to `evaluator` on save.
    """
    model_config = _BASE_CONFIG

    mode: Literal["function", "cel", "literal"] = Field(
        default="function",
        description=(
            "Evaluator mode — `'function'` (Python expression against "
            "`step_input`), `'cel'` (CEL string passed to agno verbatim, "
            "requires `cel-python`), or `'literal'` (`True`/`False` constant)."
        ),
    )
    expression: str = Field(
        default="",
        description=(
            "Dispatch expression. function mode: a Python expression wrapped "
            "as `return (<expr>)`. cel mode: a CEL expression string. "
            "literal mode: the literal `'True'` or `'False'`."
        ),
    )
    # True when this evaluator was generated from a legacy DSL string
    # (contains:/equals:/regex:). Used by the UI to show a one-time
    # "we translated your old syntax" toast.
    migrated_from_legacy: bool = Field(
        default=False,
        alias="migratedFromLegacy",
        description=(
            "True when this evaluator was auto-generated from a legacy DSL "
            "string (`contains:` / `equals:` / `regex:` / `always` / `never`). "
            "The UI surfaces a one-time toast on first edit."
        ),
    )

class ConditionNodeConfig(BaseModel):
    """ DEPRECATED — use `BranchNodeConfig`.

    Kept so `_compat.migrate_envelope` can still introspect legacy
    `type: "condition"` envelopes on the read path; new workflows
    should never see this class.
    """
    model_config = _BASE_CONFIG
    condition: str = Field(default="")
    evaluator: ConditionEvaluator = Field(default_factory=ConditionEvaluator)
    else_target: str = Field(default="", alias="elseTarget")
    requires_confirmation: bool = Field(default=False, alias="requiresConfirmation")
    confirmation_message: str = Field(default="", alias="confirmationMessage")

class BranchNodeConfig(BaseModel):
    """ — replaces standalone `router` and
    `condition` nodes.

    Mode selects between N-ary routing (`switch`, agno
    `Router(selector=...)`) and binary if/else (`if-else`, agno
    `Condition(evaluator=...)`). Both modes share the
    `branches: list[BranchTarget]` list (wired via canvas edges):

      - `switch` — every branch is a candidate; the selector picks
        one to run. Function-mode selectors return a branch STEP
        OBJECT, not a label string.
      - `if-else` — first outgoing edge is `then`; second edge OR
        `elseTarget` is `else` (matches prior `condition` semantics,
        see `app.core.ir.get_condition_branches`).

    Mode-specific fields (`selector`, `evaluator`, `elseTarget`,
    `requiresConfirmation`, `confirmationMessage`) only take effect
    in their corresponding mode and are silently ignored otherwise.
    The unified shape keeps the LLM-facing schema honest (one
    discriminated `mode` field, no two parallel config trees).
    """
    model_config = _BASE_CONFIG

    mode: Literal["switch", "if-else"] = Field(
        default="switch",
        description=(
            "`switch` = N-ary routing via `Router(selector=...)`. "
            "`if-else` = binary condition via `Condition(evaluator=...)`."
        ),
    )
    # ── switch-mode fields ─────────────────────────────────────────
    selector: RouterSelector = Field(
        default_factory=lambda: RouterSelector(mode="function"),
        description=(
            "Selector config — only effective when `mode='switch'`. "
            "Defaults to `function` mode with empty expression."
        ),
    )
    # ── if-else-mode fields ────────────────────────────────────────
    evaluator: ConditionEvaluator = Field(
        default_factory=ConditionEvaluator,
        description=(
            "Evaluator config — only effective when `mode='if-else'`. "
            "Defaults to `function` mode with empty expression. For "
            "non-trivial conditions the LLM must set both `mode` and "
            "`expression`."
        ),
    )
    else_target: str = Field(
        default="",
        alias="elseTarget",
        description=(
            "Optional explicit `else` target — only effective when "
            "`mode='if-else'`. If absent, the second outgoing edge is "
            "the else branch. Empty = no else branch."
        ),
    )
    requires_confirmation: bool = Field(
        default=False,
        alias="requiresConfirmation",
        description=(
            "Block-level HITL — only effective when `mode='if-else'`. "
            "Pauses before the condition and asks the user to confirm "
            "whether the `then` branch should run. On reject, the "
            "`else_steps` run (or the workflow skips)."
        ),
    )
    confirmation_message: str = Field(
        default="",
        alias="confirmationMessage",
        description=(
            "Custom prompt shown when `requiresConfirmation` fires. "
            "Empty = agno's built-in default."
        ),
    )
    # ── shared ─────────────────────────────────────────────────────
    branches: list[BranchTarget] = Field(
        default_factory=list,
        description=(
            "Branch targets. Wired via canvas edges — this list is a "
            "display mirror. In `switch` mode every branch is a "
            "candidate; in `if-else` mode the first edge is `then` and "
            "the second edge (or `elseTarget`) is `else`."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_condition(cls, data: Any) -> Any:
        """Back-compat: legacy `condition` DSL strings (used by the
        prior standalone `condition` node) get auto-migrated to
        `evaluator` on save. Idempotent — safe to run on every save.

        Originally introduced on `ConditionNodeConfig`; keeps it alive
        on `BranchNodeConfig` so legacy condition envelopes auto-
        upgrade when re-saved.
        """
        if not isinstance(data, dict):
            return data
        legacy = (data.get("condition") or "").strip()
        if not legacy:
            return data
        evaluator = data.get("evaluator")
        if isinstance(evaluator, dict) and evaluator.get("expression"):
            # New format wins; legacy field is silently dropped
            data = dict(data)
            data.pop("condition", None)
            return data
        # Migrate
        from app.core.compile.condition import migrate_legacy_condition
        migrated = migrate_legacy_condition(legacy)
        data = dict(data)
        data["evaluator"] = {
            "mode": migrated["mode"],
            "expression": migrated["expression"],
            "migratedFromLegacy": True,
        }
        data.pop("condition", None)
        return data

class LoopNodeConfig(BaseModel):
    """A loop re-runs its body up to `max_iterations` times, exiting
    early when `end_condition` substring-matches the last step's text.

    `max_iterations` is bounded 1..1000 — the previous code path
    silently clamped, but a user with `max_iterations=99999` was
    wasting resources. Pydantic surfaces a clear 422 now.

    Two HITL knobs, both backed by agno's `Loop.human_review`
    (which only accepts `requires_confirmation` and
    `requires_iteration_review` — see
    `agno.workflow.types.validate_human_review_for_loop`):

      - `requires_confirmation`: ask once before the loop starts.
      - `requires_iteration_review`: ask before each iteration.

    Both default to False so existing workflows keep working. The
    matching `*_message` fields are shown to the user when the prompt
    fires; empty string = agno's built-in prompt.
    """
    model_config = _BASE_CONFIG

    max_iterations: int = Field(
        default=3,
        ge=1,
        le=1000,
        alias="maxIterations",
        description=(
            "Maximum number of iterations. 1..1000. The previous code path "
            "silently clamped, but a user with `maxIterations=99999` was "
            "wasting resources — Pydantic surfaces a clear 422 now."
        ),
    )
    end_condition: str = Field(
        default="",
        alias="endCondition",
        description=(
            "Substring that, if found in the iteration's output text, ends "
            "the loop early (cheap deterministic check, no LLM). Empty = no "
            "early exit (loop runs the full `maxIterations`)."
        ),
    )
    forward_iteration_output: bool = Field(
        default=False,
        alias="forwardIterationOutput",
        description=(
            "If true, the iteration's output is fed forward as the next "
            "iteration's input (instead of the original upstream text). Lets "
            "the user build \"feedback\" loops where each iteration refines "
            "the previous one."
        ),
    )
    body_target: str = Field(
        default="",
        alias="bodyTarget",
        description=(
            "Iteration body node id. Empty = no body wired (the loop has no "
            "effect). The loop's outgoing edges are post-loop continuation, "
            "NOT the body itself — adding an edge to `bodyTarget` is a "
            "schema error (the body would execute twice)."
        ),
    )

    # HITL — two knobs, both backed by agno's Loop.human_review.
    requires_confirmation: bool = Field(
        default=False,
        alias="requiresConfirmation",
        description=(
            "Ask once before the loop starts. Backed by "
            "`Loop.human_review.requires_confirmation`."
        ),
    )
    confirmation_message: str = Field(
        default="",
        alias="confirmationMessage",
        description=(
            "Custom prompt shown when `requiresConfirmation` fires. Empty = "
            "agno's built-in default prompt."
        ),
    )
    requires_iteration_review: bool = Field(
        default=False,
        alias="requiresIterationReview",
        description=(
            "Ask before each iteration starts. Backed by "
            "`Loop.human_review.requires_iteration_review`. Pair with "
            "`requiresConfirmation=False` for a clean review-only loop."
        ),
    )
    iteration_review_message: str = Field(
        default="",
        alias="iterationReviewMessage",
        description=(
            "Custom prompt shown when `requiresIterationReview` fires. "
            "Empty = agno's built-in default prompt."
        ),
    )

class AskConfig(BaseModel):
    """Config for an Ask node — VALUE COLLECTOR (not a branch decider).

    Renamed from `HumanInputNodeConfig`. The node identity is now
    `ask`; `kind` is `control_flow` (was `executable`). Wraps
    `Step(requires_user_input=True)` from agno 2.8.7. The runtime
    pauses the pipeline so the user can supply
    typed input, then injects the answer downstream as this step's
    output. The workflow ALWAYS continues into whatever is wired
    downstream — `ask` does not choose a branch. If the user's answer
    needs to route to different downstream nodes (yes/no decisions,
    single-choice pick across N options), use a `Branch` with
    `selector.mode='hitl'` instead. See the HITL section in the
    chat-builder system prompt for the full decision rule.
    """
    model_config = _BASE_CONFIG

    prompt: str = Field(
        default="",
        description=(
            "Question shown to the user when the step pauses for input. "
            "Free text. If empty, the runtime shows a generic prompt."
        ),
    )
    input_type: Literal["text", "confirm", "choice"] = Field(
        default="text",
        alias="inputType",
        description=(
            "Shape of user input. `text`=free text. `confirm`=yes/no "
            "(user types //yes/no; answer becomes step output). "
            "`choice`=single-select from `choices[]`. ⚠️ In `text`/`confirm` "
            "modes the answer is fed downstream as text only — it does NOT "
            "decide a branch. For branch routing use a `router` with "
            "`selector.mode='hitl'`."
        ),
    )
    choices: list[str] = Field(
        default_factory=list,
        description=(
            "Only used when `inputType='choice'`. The list of options "
            "the user can pick from. Order is preserved. Empty list "
            "with `inputType='choice'` raises a validation error."
        ),
    )

# ─────────────────────────────────────────────────────────────────
# Type → schema dispatch
# ─────────────────────────────────────────────────────────────────
# The per-type schemas (the union members) are still defined
# above so Pydantic can build the discriminated `NodeConfig`
# union at import time. The runtime dispatch table `NODE_CONFIG_SCHEMA`
# is now derived from `shared/nodes.manifest.json` so adding a new
# node type is a one-line manifest change (plus the schema class here
# if it's a new schema). The `WorkflowNode` validator consumes this
# dict to pick the right validator for `data.config`.
NodeConfig = Annotated[
    Union[
        AgentNodeConfig,
        # `McpNodeConfig` + `HttpNodeConfig` + `ToolsNodeConfig` all
        # collapse into a single `ToolNodeConfig` with a `source`
        # discriminator.
        ToolNodeConfig,
        BranchNodeConfig,
        FlowNodeConfig,
        LoopNodeConfig,
        AskConfig,
    ],
    Field(discriminator="__discriminator__"),
]

# Module-level lazy proxy: build the dict from the manifest on first
# access. This is a `__getattr__` shim so any consumer that does
# `from app.schemas.node_configs import NODE_CONFIG_SCHEMA; NODE_CONFIG_SCHEMA["agent"]`
# keeps working without code changes. The dict itself is cached in
# `app.core.node_types.node_config_schema()`.
def __getattr__(name: str):
    if name == "NODE_CONFIG_SCHEMA":
        from app.core.node_types import node_config_schema
        return node_config_schema()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# ─────────────────────────────────────────────────────────────────
# row M  — static strict siblings for the LLM
# write path. Functionally identical to the previous
# `type("Strict…", (cls,), {"model_config": _LLM_WRITE_CONFIG})`
# dynamic-generation pattern, but declared as real subclasses so
# they're importable (`from app.schemas.node_configs import
# AgentNodeConfigLLM`) and visible to type checkers.
#
# Read-time lax behaviour of the originals is preserved (the originals
# still have `extra="ignore"`). The strict siblings exist ONLY for
# `chat_builder_plan.validate_node_config_for_llm` to surface unknown-
# field errors as `Issue`s with did-you-mean hints.
# ─────────────────────────────────────────────────────────────────
class AgentNodeConfigLLM(AgentNodeConfig):
    model_config = _LLM_WRITE_CONFIG

# `McpNodeConfigLLM` + `HttpNodeConfigLLM` +
# `ToolsNodeConfigLLM` collapsed to a single `ToolNodeConfigLLM`
# (a strict sibling pattern with a parallel LLM-write config).
# One class covers all 3 `source` modes; the LLM writes
# `config.source='http'|'mcp'|'function'` and Pydantic surfaces
# unknown-field errors with did-you-mean hints against the
# unified field set.
class ToolNodeConfigLLM(ToolNodeConfig):
    model_config = _LLM_WRITE_CONFIG

class BranchNodeConfigLLM(BranchNodeConfig):
    model_config = _LLM_WRITE_CONFIG

class FlowNodeConfigLLM(FlowNodeConfig):
    model_config = _LLM_WRITE_CONFIG

class LoopNodeConfigLLM(LoopNodeConfig):
    model_config = _LLM_WRITE_CONFIG

class AskConfigLLM(AskConfig):
    model_config = _LLM_WRITE_CONFIG

# Node-type → strict sibling class. Replaces the lazy dynamic registry.
# Keyed by the `configSchemaRef` values in `shared/nodes.manifest.json`.
_NODE_TYPE_TO_STRICT_SCHEMA: dict[str, type[BaseModel]] = {
    "agent": AgentNodeConfigLLM,
    # `http` / `mcp` / `tools` all collapse to `tool` (the source
    # discriminator). Legacy alias rows (`http` → `tool`, `mcp` →
    # `tool`, `tools` → `tool`) are resolved in `get_strict_schema` via
    # `LEGACY_NODE_ALIASES` so legacy envelopes still validate.
    "tool": ToolNodeConfigLLM,
    "branch": BranchNodeConfigLLM,
    "flow": FlowNodeConfigLLM,
    "loop": LoopNodeConfigLLM,
    "ask": AskConfigLLM,
}

# Eagerly populate the module attribute on first import so static
# type checkers / `dir()` see it. Using `sys.modules` to set it once.
# This mirrors the lazy `__getattr__` above — both paths converge.
_NODE_CONFIG_SCHEMA_CACHE: dict[str, type[BaseModel]] | None = None

def _get_node_config_schema() -> dict[str, type[BaseModel]]:
    """Eager accessor used by `validate_node_config` (avoids the
    `__getattr__` indirection on every call)."""
    global _NODE_CONFIG_SCHEMA_CACHE
    if _NODE_CONFIG_SCHEMA_CACHE is None:
        from app.core.node_types import node_config_schema
        _NODE_CONFIG_SCHEMA_CACHE = node_config_schema()
    return _NODE_CONFIG_SCHEMA_CACHE

# ─────────────────────────────────────────────────────────────────
# Strict sibling schemas for the LLM tool surface
# ─────────────────────────────────────────────────────────────────
# The lax `_BASE_CONFIG` ignores unknown fields; that keeps saved
# workflows loading even when the schema has moved on. But for the
# LLM builder we want WRONG-SHAPE configs to surface as a typed
# `Issue` with a "did you mean" hint so the LLM self-corrects on the
# next attempt — silently dropping fields hides its mistakes.
#
# We achieve this by generating a parallel strict sibling per type:
#   `StrictAgentNodeConfig(AgentNodeConfig)` with `extra="forbid"`.
# Same shape, same validators, same aliases — only the extra-fields
# policy differs. The lax original is untouched so read-time loading
# (and `test_extra_fields_silently_ignored`) keeps working.
_STRICT_SCHEMA_CACHE: dict[str, type[BaseModel]] | None = None

def _get_strict_schema_registry() -> dict[str, type[BaseModel]]:
    """Return the static strict sibling registry (row M).

    Previously this lazily generated siblings via
    `type("Strict…", (cls,), {"model_config": _LLM_WRITE_CONFIG})`
    on first access. Now those siblings are declared as real
    subclasses (`AgentNodeConfigLLM` etc.) so type checkers can see
    them — this function is a thin pointer to the module-level dict.
    """
    return _NODE_TYPE_TO_STRICT_SCHEMA

def get_strict_schema(node_type: str) -> type[BaseModel] | None:
    """Return the strict sibling class for `node_type`, or `None`
    if the type isn't in the manifest. The strict class rejects
    unknown fields with `extra_forbidden` validation errors so the
    chat-builder can surface them as `Issue`s with hint text.

    Legacy aliases (`parallel`, `steps`, `router`, `condition`)
    are translated to their merged target (`flow`, `branch`)
    before the registry lookup, so an LLM that still emits the
    old type names gets the same strict gate instead of a silent
    pass-through.
    """
    # Lazy import — `node_configs` already has many dependents and
    # _compat pulls in only the constants we need here.
    from app.core._compat import LEGACY_NODE_ALIASES
    if node_type in LEGACY_NODE_ALIASES:
        node_type = LEGACY_NODE_ALIASES[node_type][0]
    return _NODE_TYPE_TO_STRICT_SCHEMA.get(node_type)

def _did_you_mean(unknown: str, candidates: Iterable[str]) -> str:
    """Pick the closest candidate name to `unknown` using
    `difflib.get_close_matches`. Returns the suggestion or `""` if
    none is close enough (cutoff 0.6).

    The LLM is on a tight context budget, so we only return ONE
    suggestion — the top match. Empty return signals "no good
    match" and the caller can fall back to the generic
    `get_node_types()` hint.
    """
    from difflib import get_close_matches
    matches = get_close_matches(unknown, list(candidates), n=1, cutoff=0.6)
    return matches[0] if matches else ""

__all__ = [
    "ModelConfig",
    "AgentNodeConfig",
    # `McpNodeConfig` + `HttpNodeConfig` + `ToolsNodeConfig` all
    # collapse into a single `ToolNodeConfig`.
    "ToolNodeConfig",
    "ParamSchema",
    "ToolFunction",
    "BranchTarget",
    "RouterNodeConfig",
    "FlowNodeConfig",
    "ConditionNodeConfig",
    "LoopNodeConfig",
    "AskConfig",
    "NodeConfig",
    "NODE_CONFIG_SCHEMA",
    # row M — strict siblings importable for type checkers
    "AgentNodeConfigLLM",
    "ToolNodeConfigLLM",
    "BranchNodeConfigLLM",
    "FlowNodeConfigLLM",
    "LoopNodeConfigLLM",
    "AskConfigLLM",
    # Write-time strict sibling accessors
    "get_strict_schema",
    "_did_you_mean",
]

# ─────────────────────────────────────────────────────────────────
# Helper: validate a raw config dict against the schema for `node_type`.
#
# Used by `WorkflowNode`'s model_validator AND exposed for the import-
# json path (`workflow_service.import_json`) so the same validation
# runs whether the workflow is created fresh or restored from a
# versioned envelope.
# ─────────────────────────────────────────────────────────────────
def validate_node_config(node_type: str, config: Any) -> Any:
    """Return a typed config object, or pass through if the type is
    unknown / config is empty / config is not a dict.

    The pass-through is intentional: `input`/`output` aren't real
    types anymore (kept in the type union for legacy JSON), and an
    empty config means "the user hasn't filled it yet" — we don't
    want to 422 on every save while the canvas is half-edited.

    The schema lookup is manifest-driven: see
    `app.core.node_types.node_config_schema`.
    """
    if not isinstance(config, dict):
        return config
    schema_cls = _get_node_config_schema().get(node_type)
    if schema_cls is None:
        return config
    return schema_cls.model_validate(config)