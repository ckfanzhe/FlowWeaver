"""AgentStrategy — `Agent(name, model, instructions, ...)` + `Step(agent=...)`.

The `tools=[...]` wiring is handled by the pipeline in pass 3 (after
every tool node has been declared). This class only owns the agent
object + the Step wrapper.

The runtime passes the tools list in via the `ctx` populated by the
pipeline — `build_agent(...)` accepts the tools list directly so the
pipeline can do per-node tool wiring. The export emits the same list
verbatim from the `tools_expr` template.

Wires 11 additional fields through to `Agent(...)` (reasoning,
retries, parser_model, hooks, …). All have safe defaults so legacy
workflows emit the same code as before. Hooks are resolved from
`ctx.tool_objects` at *build* time and from the source-tree at
*export* time — see `tools_expr.hooks_expr`.
"""
from __future__ import annotations

from typing import Any, ClassVar, Literal, Optional

from .base import NodeStrategy

def _build_optional_model(
    model_cfg: Optional[dict],
    *,
    user_id: Optional[str],
) -> Optional[Any]:
    """Build an agno Model from cfg, returning None on failure.

    Used for `reasoning_model` / `parser_model` — these are optional
    sub-models; if the user picked a preset that doesn't exist, we
    silently skip (agno will run without the auxiliary model) rather
    than failing the whole workflow.
    """
    if not model_cfg:
        return None
    # Lazy — keeps `app.core.llm_runner` out of the module-level import
    # graph so importing `app.core.strategies` doesn't transitively
    # pull in `app.core.compile` (which imports `app.core.node_types`).
    from app.core.llm_runner import build_model
    try:
        return build_model(model_cfg, user_id=user_id)
    except Exception:
        return None

def _resolve_hooks(
    hook_refs: list[str],
    ctx: Any,
) -> list:
    """Resolve a list of `tools` node IDs into the matching callables.

    Pulls from `ctx.tool_objects[ref]` — populated by the same pipeline
    pass that builds the `tools` node (pass 0). Dangling refs (deleted
    source node) are silently skipped — the schema accepts IDs that
    don't resolve at compile time.
    """
    out: list = []
    for ref in hook_refs or []:
        for tool in (ctx.tool_objects.get(ref) or []):
            # `tool` is either a `Function` (a callable wrapper around
            # the user's function) or an `MCPTools` instance. Only the
            # former is a sensible hook — we skip non-Function objects.
            # The plain `tools` node always produces `Function` objects,
            # so this branch is taken for the only supported hook kind.
            if hasattr(tool, "function") and callable(getattr(tool, "function", None)):
                out.append(tool.function)
            elif callable(tool):
                out.append(tool)
    return out

class AgentStrategy(NodeStrategy):
    """`Agent(name, model, instructions, ...)` + `Step(agent=...)`.

    The `tools=[...]` wiring is owned by the pipeline's pass 3 — this
    strategy just builds the agent object. The pipeline calls
    `build(..., tools=[...])` with the wired tools list (the agent
    emitter today accepts the same keyword arg).

    Note: `app.core.*` helpers (`CompileError`, `build_model`,
    `model_expr`, `repr_instructions`, etc.) are imported lazily inside
    `build()` / `to_source()` rather than at module top — keeps the
    strategy module's import graph acyclic so the manifest loader can
    import it before `app.core.node_types.NODE_TYPES` is bound.
    """

    KIND: ClassVar[Literal["executable", "compound", "tool_source", "control_flow"]] = "executable"
    COMPOUND_PASS: ClassVar[Optional[int]] = None
    IS_TOOL_SOURCE: ClassVar[bool] = False
    NEEDS_TOOL_WIRING: ClassVar[bool] = True
    STEP_WRAPPER: ClassVar[Literal["agent", "ask", "none"]] = "agent"

    def build(
        self,
        nid: str,
        node: dict,
        ctx: Any,
        *,
        tools: Optional[list] = None,
    ) -> Any:
        """Build an `Agent` instance for an agent node.

        Returns the `Agent` (NOT the Step wrapper). The pipeline wraps it
        in `Step(name=..., agent=...)` for executable types.

        Model resolution :

          1. Try the inline `cfg.model` first. A complete inline config
             (preset OR provider+modelId+apiKey) wins — this preserves
             any per-agent override a user typed in directly.
          2. If the inline config didn't yield a buildable Model (e.g.
             the built-in templates carry a legacy `provider+modelId`
             stub with no API key), fall back to the workflow owner's
             default `LlmPreset`. The per-agent model dropdown was
             removed from the PropertyPanel, so an inline config that
             can't build a Model is always a stub the default preset
             should satisfy.
          3. If neither path produces a Model, raise a clear
             `CompileError` pointing the user at Settings → LLM Models.
        """
        from agno.agent import Agent
        from app.core.compile.errors import CompileError
        from app.core.llm_runner import build_model, _resolve_default_preset_id

        cfg = node["data"].get("config") or {}
        label = node["data"].get("label") or nid
        model_cfg = cfg.get("model") or {}
        # Scope LLM preset resolution to the workflow owner so alice's
        # default preset doesn't accidentally serve bob's agents.
        # `ctx.user_id` is set by `compile.build_workflow(...)` from
        # `workflow.created_by`.
        user_id = getattr(ctx, "user_id", None)

        # Layer 1: inline config wins when it actually builds a Model.
        # This branch is rarely exercised — most user-authored nodes
        # leave `cfg.model` empty — but a power user can still pin a
        # specific preset/model per agent and we honour it.
        model = build_model(model_cfg, user_id=user_id) if model_cfg else None

        # Layer 2: if the inline config didn't yield a Model, fall back
        # to the workflow owner's default `LlmPreset`. This is the
        # round-2 fix : previously the agent emitter only
        # consulted the default preset when `model_cfg` was entirely
        # missing, which made every built-in template (e.g.
        # `tpl-hello-world` whose agent carries a legacy
        # `{provider, modelId}` stub with no apiKey) fail to compile for
        # a logged-in user even after they'd configured a default
        # preset — `build_model` would return None and the emitter
        # raised "Agent has no model". See the regression test
        # `test_agent_emitter_falls_back_to_default_preset_when_inline_config_is_incomplete`.
        if model is None:
            preset_id = _resolve_default_preset_id(user_id=user_id)
            if preset_id:
                model = build_model({"presetId": preset_id}, user_id=user_id)

        if model is None:
            raise CompileError(
                f"Agent '{label}' has no model — set a "
                "default LLM preset in Settings → LLM Models"
            )
        label = node["data"].get("label") or nid
        instructions = cfg.get("instructions") or "You are a helpful assistant."
        markdown = bool(cfg.get("markdown", True))

        # Auxiliary sub-models. None when missing / unbuildable (rather
        # than raising — the agent still works as a plain chat agent).
        reasoning_model = _build_optional_model(
            cfg.get("reasoningModel"), user_id=user_id
        )
        parser_model = _build_optional_model(
            cfg.get("parserModel"), user_id=user_id
        )

        # Hooks — resolved from `ctx.tool_objects` (the same data the
        # pipeline uses to wire `tools=[...]`). Plain callables only.
        pre_hooks = _resolve_hooks(cfg.get("preHooks") or [], ctx)
        post_hooks = _resolve_hooks(cfg.get("postHooks") or [], ctx)

        agent_kwargs: dict = {
            "name": label,
            "model": model,
            "instructions": instructions,
            "markdown": markdown,
            "tools": list(tools or []),
            #  (session — runtime multi-turn context fix):
            # always inject prior messages into the agent's prompt so a
            # follow-up user message after a tool-calling turn can see
            # the prior tool calls + tool results (e.g. "type the
            # confirmation" can call `dispatch_task` with the substations
            # the prior `query_substations` call returned). Without this,
            # agno's default `add_history_to_context=False` means the
            # agent re-runs every turn as if it's a fresh conversation.
            # The runtime already reuses the slim + agno session across
            # turns (see `runtime_service._run_leg`); this is the
            # matching agent-side wiring that makes the prior context
            # actually visible to the LLM.
            "add_history_to_context": True,
            # / session: number of prior runs to inject as
            # history. Default 5 (kept as the v1 default per the
            # session rationale). Power users can override per-agent
            # via `cfg.numHistoryRuns` — the AgentNodeConfig
            # schema caps the field at 50 to prevent runaway-prompt
            # abuse. The trade-off is prompt size: each prior run
            # carries its full tool-call transcript, so 10 runs of
            # a tool-heavy agent can blow up the prompt size. The
            # cap is the user's lever, not a runtime auto-tune.
            "num_history_runs": cfg.get("numHistoryRuns") or 5,
        }
        # Only inject the auxiliary kwargs when the user opted in, so
        # the runtime object matches what `to_source()` emits
        # (byte-equivalent for the no-op path).
        if cfg.get("systemMessage"):
            agent_kwargs["system_message"] = cfg["systemMessage"]
        if cfg.get("reasoning"):
            agent_kwargs["reasoning"] = True
            if reasoning_model is not None:
                agent_kwargs["reasoning_model"] = reasoning_model
        if cfg.get("retries"):
            agent_kwargs["retries"] = int(cfg["retries"])
        if cfg.get("delayBetweenRetries") and cfg.get("retries"):
            agent_kwargs["delay_between_retries"] = int(cfg["delayBetweenRetries"])
        if cfg.get("toolCallLimit") is not None:
            agent_kwargs["tool_call_limit"] = int(cfg["toolCallLimit"])
        if cfg.get("addDatetimeToContext"):
            agent_kwargs["add_datetime_to_context"] = True
        if parser_model is not None:
            agent_kwargs["parser_model"] = parser_model
            if cfg.get("parserModelPrompt"):
                agent_kwargs["parser_model_prompt"] = cfg["parserModelPrompt"]
        if pre_hooks:
            agent_kwargs["pre_hooks"] = pre_hooks
        if post_hooks:
            agent_kwargs["post_hooks"] = post_hooks

        return Agent(**agent_kwargs)

    def to_source(self, nid: str, node: dict, ctx: Any) -> str:
        """Emit `<nid>_agent = Agent(...)` and `<nid>_step = Step(...)`."""
        from app.core.compile.errors import CompileError
        from app.core.compile._helpers.models import model_expr
        from app.core.compile._helpers.tools_expr import hooks_expr
        from app.core.compile._helpers.utils import q, repr_instructions

        cfg = node["data"].get("config") or {}
        if not cfg.get("model"):
            raise CompileError(f"agent node {nid!r} missing model config")
        label = node["data"].get("label") or nid
        label_repr = q(label)

        # Emit the same kwargs `build()` injects. Order matches the
        # `Agent.__init__` signature so the export stays readable. Each
        # kwargs block is only emitted when the user opted in.
        extra_kwargs: list[str] = []
        # Multi-turn context: always emit `add_history_to_context=True`
        # so the EXPORTED .py behaves identically to the runtime
        # build(). Without this, an exported workflow re-loaded into the
        # runtime (or run standalone via `Wf.run()`) would drop the
        # multi-turn-context behaviour — runtime and export would
        # diverge on
        # a user-visible axis. See `build()` above for rationale.
        #
        #  (session — configurable num_history_runs): emit
        # the user-configured value (default 5). This pins the
        # runtime/export contract for the configurable field —
        # if the user sets `numHistoryRuns: 20` in the canvas, the
        # exported .py must use 20 too.
        extra_kwargs.append("    add_history_to_context=True,")
        num_history_runs = cfg.get("numHistoryRuns") or 5
        extra_kwargs.append(f"    num_history_runs={int(num_history_runs)},")
        # RAG / knowledge wiring — emit `search_knowledge=True` always
        # when a knowledge is attached (matches runtime behavior where
        # pass 3b sets `agent.knowledge = kb`; the agent gets a
        # `search_knowledge` tool it can invoke). `knowledge=<ref>_kb`
        # is set by `_pass3_knowledge_wiring_source` as a separate line
        # (parallel to how `tools=` is set by pass 3 source — see the
        # `tools=[...]` comment below). `add_knowledge_to_context=True`
        # propagates from the knowledge node's cfg when the user opted
        # in (auto-inject retrieved chunks into the prompt instead of
        # relying on the agent to call `search_knowledge`).
        knowledge_refs = ctx.ir.knowledge_attachments.get(nid) or []
        if knowledge_refs:
            extra_kwargs.append("    search_knowledge=True,")
            kb_node = ctx.nodes_by_id.get(knowledge_refs[0]) or {}
            kb_cfg = (kb_node.get("data") or {}).get("config") or {}
            if kb_cfg.get("addKnowledgeToContext"):
                extra_kwargs.append("    add_knowledge_to_context=True,")
        if cfg.get("systemMessage"):
            extra_kwargs.append(f"    system_message={repr_instructions(cfg['systemMessage'])},")
        if cfg.get("reasoning"):
            # reasoning_model is only emitted if the user also filled a model
            if cfg.get("reasoningModel"):
                extra_kwargs.append(f"    reasoning_model={model_expr(cfg['reasoningModel'])},")
            extra_kwargs.append("    reasoning=True,")
        if cfg.get("retries"):
            extra_kwargs.append(f"    retries={int(cfg['retries'])},")
            if cfg.get("delayBetweenRetries"):
                extra_kwargs.append(
                    f"    delay_between_retries={int(cfg['delayBetweenRetries'])},"
                )
        if cfg.get("toolCallLimit") is not None:
            extra_kwargs.append(f"    tool_call_limit={int(cfg['toolCallLimit'])},")
        if cfg.get("addDatetimeToContext"):
            extra_kwargs.append("    add_datetime_to_context=True,")
        if cfg.get("parserModel"):
            extra_kwargs.append(f"    parser_model={model_expr(cfg['parserModel'])},")
            if cfg.get("parserModelPrompt"):
                extra_kwargs.append(
                    f"    parser_model_prompt={repr_instructions(cfg['parserModelPrompt'])},"
                )
        pre_hooks_refs = cfg.get("preHooks") or []
        if pre_hooks_refs:
            extra_kwargs.append(
                f"    pre_hooks={hooks_expr(pre_hooks_refs, ctx.nodes_by_id)},"
            )
        post_hooks_refs = cfg.get("postHooks") or []
        if post_hooks_refs:
            extra_kwargs.append(
                f"    post_hooks={hooks_expr(post_hooks_refs, ctx.nodes_by_id)},"
            )
        extra_block = "\n".join(extra_kwargs)
        if extra_block:
            extra_block = extra_block + "\n"

        return (
            f"{nid}_agent = Agent(\n"
            f"    name={label_repr},\n"
            f"    model={model_expr(cfg['model'])},\n"
            f"    instructions={repr_instructions(cfg.get('instructions'))},\n"
            f"    markdown={'True' if cfg.get('markdown') else 'False'},\n"
            f"{extra_block}"
            f")\n"
            f"{nid}_step = Step(name={label_repr}, agent={nid}_agent)\n"
        )

__all__ = ["AgentStrategy"]