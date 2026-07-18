"""Per-node-config schema validation tests.

Each node type has a Pydantic schema in `app.schemas.node_configs`.
The validation runs at the API boundary (POST/PUT/PATCH on workflows)
via a `model_validator` on `WorkflowNode` — bad shapes get a 422 with
a clear field-level error before any DB write happens.

The tests pin:

  - Per-type rejection: every schema that should reject bad input does
    reject it (e.g. `loop.maxIterations > 1000`, `http.baseUrl` empty
    AND `method=POST`, `agent.model.provider` unknown).
  - Tolerance: empty `data` and unknown extra fields don't 422 — the
    canvas is mid-edit half the time and dropping a save with a 422
    because of one stray field would be hostile UX.
  - End-to-end: a malformed payload hitting `POST /workflows` returns
    a 422 with a structured `detail` body (FastAPI / Pydantic default).

Why this file exists:
  - Before typed configs landed, `WorkflowNode.data.config` was `dict[str, Any]`. Bad
    shapes only failed at runtime (or worse, at code export). The
    frontend's `NodeConfig` union was structurally a `any` too — typos
    slipped through `PropertyPanel` into the DB.
  - These tests guard the new gate so a future schema loosening can't
    silently re-open the door.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.node_configs import (
    AgentNodeConfig,
    BranchNodeConfig,
    LoopNodeConfig,
    ModelConfig,
    ToolNodeConfig,
    validate_node_config,
)
from app.schemas import node_configs as _node_configs_mod

def from_app_schemas(name: str):
    """Helper: pull a schema class by name from
    `app.schemas.node_configs`. Keeps the test file's import list
    short while still referencing every nested type (ParamSchema,
    ToolFunction, BranchTarget, …) the LLM needs docs for."""
    cls = getattr(_node_configs_mod, name)
    return cls

# ─────────────────────────────────────────────────────────────────
# Agent
# ─────────────────────────────────────────────────────────────────
class TestAgentConfig:
    def test_minimal_valid(self):
        cfg = AgentNodeConfig.model_validate({"instructions": "hi"})
        assert cfg.instructions == "hi"
        assert cfg.markdown is True
        assert cfg.tools_ref == []

    def test_with_full_model(self):
        cfg = AgentNodeConfig.model_validate({
            "model": {"provider": "openai", "modelId": "gpt-4o"},
            "instructions": "answer in one sentence",
        })
        assert cfg.model is not None
        assert cfg.model.provider == "openai"
        assert cfg.model.model_id == "gpt-4o"

    def test_unknown_provider_is_string_typed_not_rejected(self):
        """Provider is a free-form string on the schema so future
        providers can be added without a backend release. Validation
        happens at the LLM layer, not at save time."""
        cfg = AgentNodeConfig.model_validate({
            "model": {"provider": "mystery-provider", "modelId": "x"},
        })
        assert cfg.model and cfg.model.provider == "mystery-provider"

    def test_extra_fields_silently_ignored(self):
        """Forward-compat: unknown fields don't break the save."""
        cfg = AgentNodeConfig.model_validate({
            "instructions": "hi",
            "futureField": "ignored",
            "model": {"provider": "openai", "modelId": "gpt-4o", "wildKey": 42},
        })
        assert "futureField" not in cfg.model_dump()
        assert not hasattr(cfg.model, "wildKey")

    # ─── Agent field extensions ────────────────────────────────────
    def test_phase91_defaults_all_safe(self):
        """All 11 new fields default to safe values so existing
        workflows keep validating without migration."""
        cfg = AgentNodeConfig.model_validate({})
        assert cfg.system_message == ""
        assert cfg.reasoning is False
        assert cfg.reasoning_model is None
        assert cfg.retries == 0
        assert cfg.delay_between_retries == 1
        assert cfg.tool_call_limit is None
        assert cfg.add_datetime_to_context is False
        assert cfg.parser_model is None
        assert cfg.parser_model_prompt == ""
        assert cfg.pre_hooks == []
        assert cfg.post_hooks == []

    def test_phase91_camel_case_accepted(self):
        """Frontend serializes as camelCase; aliases round-trip."""
        cfg = AgentNodeConfig.model_validate({
            "systemMessage": "Be terse.",
            "reasoning": True,
            "retries": 3,
            "delayBetweenRetries": 5,
            "toolCallLimit": 10,
            "addDatetimeToContext": True,
            "parserModelPrompt": "Return JSON.",
            "preHooks": ["tools_a", "tools_b"],
            "postHooks": ["tools_c"],
        })
        assert cfg.system_message == "Be terse."
        assert cfg.reasoning is True
        assert cfg.retries == 3
        assert cfg.delay_between_retries == 5
        assert cfg.tool_call_limit == 10
        assert cfg.add_datetime_to_context is True
        assert cfg.parser_model_prompt == "Return JSON."
        assert cfg.pre_hooks == ["tools_a", "tools_b"]
        assert cfg.post_hooks == ["tools_c"]

    def test_phase91_retries_bounds_enforced(self):
        """retries must be 0..10 — typos like 9999 surface as 422."""
        with pytest.raises(ValidationError):
            AgentNodeConfig.model_validate({"retries": 11})
        with pytest.raises(ValidationError):
            AgentNodeConfig.model_validate({"retries": -1})

    def test_phase91_delay_between_retries_bounds_enforced(self):
        """delay_between_retries must be 0..60."""
        with pytest.raises(ValidationError):
            AgentNodeConfig.model_validate({"delayBetweenRetries": 61})
        with pytest.raises(ValidationError):
            AgentNodeConfig.model_validate({"delayBetweenRetries": -1})

    def test_phase91_tool_call_limit_bounds_enforced(self):
        """tool_call_limit must be 1..1000 or None. None = unlimited."""
        with pytest.raises(ValidationError):
            AgentNodeConfig.model_validate({"toolCallLimit": 0})
        with pytest.raises(ValidationError):
            AgentNodeConfig.model_validate({"toolCallLimit": 1001})
        # None is allowed (means "no limit")
        cfg = AgentNodeConfig.model_validate({"toolCallLimit": None})
        assert cfg.tool_call_limit is None

    def test_phase91_sub_models_optional(self):
        """reasoning_model and parser_model accept the same ModelConfig
        shape as the main `model` field — but are independently optional."""
        cfg = AgentNodeConfig.model_validate({
            "reasoningModel": {"provider": "openai", "modelId": "o1-mini"},
            "parserModel": {"provider": "openai", "modelId": "gpt-4o-mini"},
        })
        assert cfg.reasoning_model is not None
        assert cfg.reasoning_model.provider == "openai"
        assert cfg.parser_model is not None
        assert cfg.parser_model.model_id == "gpt-4o-mini"

# ─────────────────────────────────────────────────────────────────
# Flow () — replaces `StepsConfig` / `ParallelConfig`.
# `mode` discriminates between the parallel fan-out and the ordered
# pipeline; the HITL kwargs (`requiresConfirmation`,
# `confirmationMessage`) are only effective in `mode='sequential'`.
# ─────────────────────────────────────────────────────────────────
class TestFlowConfig:
    def test_minimal_valid(self):
        """Empty config is valid — branches default to []. The IR
        populates `branches` from edges; the schema is a marker."""
        from app.schemas.node_configs import FlowNodeConfig
        cfg = FlowNodeConfig.model_validate({})
        assert cfg.mode == "parallel"
        assert cfg.branches == []
        assert cfg.requires_confirmation is False
        assert cfg.confirmation_message == ""

    def test_camel_case_round_trip(self):
        """Frontend uses `requiresConfirmation` / `confirmationMessage`."""
        from app.schemas.node_configs import FlowNodeConfig
        cfg = FlowNodeConfig.model_validate({
            "mode": "sequential",
            "requiresConfirmation": True,
            "confirmationMessage": "Run the pipeline?",
        })
        assert cfg.mode == "sequential"
        assert cfg.requires_confirmation is True
        assert cfg.confirmation_message == "Run the pipeline?"

    def test_hitl_only_flag_is_accepted(self):
        """`requiresConfirmation=True` without a message stores the
        flag — the message stays empty so the emitter uses agno's
        default prompt."""
        from app.schemas.node_configs import FlowNodeConfig
        cfg = FlowNodeConfig.model_validate({"mode": "sequential", "requiresConfirmation": True})
        assert cfg.requires_confirmation is True
        assert cfg.confirmation_message == ""

# ─────────────────────────────────────────────────────────────────
# Loop
# ─────────────────────────────────────────────────────────────────
class TestLoopConfig:
    def test_defaults(self):
        cfg = LoopNodeConfig.model_validate({})
        assert cfg.max_iterations == 3
        assert cfg.forward_iteration_output is False
        assert cfg.end_condition == ""

    def test_max_iterations_too_high_rejected(self):
        """The previous code path silently clamped to 1..1000 — a
        typo of 99999 would waste compute. Now: Pydantic surfaces a
        clear 422 at save time."""
        with pytest.raises(ValidationError) as exc_info:
            LoopNodeConfig.model_validate({"maxIterations": 99999})
        assert "max_iterations" in str(exc_info.value).lower() or "maxIterations" in str(exc_info.value)

    def test_max_iterations_zero_rejected(self):
        with pytest.raises(ValidationError):
            LoopNodeConfig.model_validate({"maxIterations": 0})

    def test_max_iterations_negative_rejected(self):
        with pytest.raises(ValidationError):
            LoopNodeConfig.model_validate({"maxIterations": -1})

    def test_max_iterations_at_boundary_ok(self):
        cfg = LoopNodeConfig.model_validate({"maxIterations": 1000})
        assert cfg.max_iterations == 1000

    # ─── Loop HITL ────────────────────────────────────────────────
    def test_hitl_defaults_all_false(self):
        """The 4 new HITL fields default to False / '' so existing
        workflows keep working without migration."""
        cfg = LoopNodeConfig.model_validate({})
        assert cfg.requires_confirmation is False
        assert cfg.confirmation_message == ""
        assert cfg.requires_iteration_review is False
        assert cfg.iteration_review_message == ""

    def test_hitl_camel_case_accepted(self):
        """Frontend serializes these as camelCase keys; Pydantic's
        `populate_by_name=True` lets the alias flow both ways."""
        cfg = LoopNodeConfig.model_validate({
            "requiresConfirmation": True,
            "confirmationMessage": "About to start the loop. Continue?",
            "requiresIterationReview": True,
            "iterationReviewMessage": "Next iteration OK?",
        })
        assert cfg.requires_confirmation is True
        assert cfg.confirmation_message == "About to start the loop. Continue?"
        assert cfg.requires_iteration_review is True
        assert cfg.iteration_review_message == "Next iteration OK?"

    def test_hitl_flags_independent(self):
        """Each HITL flag can be turned on without the other."""
        cfg = LoopNodeConfig.model_validate({"requiresIterationReview": True})
        assert cfg.requires_confirmation is False
        assert cfg.requires_iteration_review is True
        # messages stay empty when only the flag is set
        assert cfg.confirmation_message == ""
        assert cfg.iteration_review_message == ""

# ─────────────────────────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────────────────────────
class TestHttpConfig:
    """: `HttpNodeConfig` + `McpNodeConfig` +
    `ToolsNodeConfig` collapsed into `ToolNodeConfig` with `source`
    discriminator. HTTP fields still exist on `ToolNodeConfig` —
    these tests pin the source='http' shape."""

    def test_minimal(self):
        cfg = ToolNodeConfig.model_validate({"source": "http"})
        assert cfg.method == "GET"
        assert cfg.base_url == ""

    def test_method_must_be_get_or_post(self):
        """The frontend's dropdown only offers GET/POST — Pydantic
        protects against a hand-edited JSON sneaking `DELETE`/etc."""
        with pytest.raises(ValidationError):
            ToolNodeConfig.model_validate({"source": "http", "method": "DELETE"})
        with pytest.raises(ValidationError):
            ToolNodeConfig.model_validate({"source": "http", "method": "PUT"})

    def test_headers_default_empty_dict(self):
        cfg = ToolNodeConfig.model_validate({"source": "http"})
        assert cfg.headers == {}

    def test_default_source_is_function(self):
        """A freshly-dropped `tool` node should default to `function`
        source — the no-op empty-functions shape — so the canvas
        doesn't auto-bind HTTP or MCP state at spawn."""
        cfg = ToolNodeConfig.model_validate({})
        assert cfg.source == "function"

# ─────────────────────────────────────────────────────────────────
# Branch — if-else mode (replaces TestConditionConfig after ,
#  — `router` + `condition` → `branch` collapse)
#
# The `branch` node type with `mode='if-else'` keeps all the prior
# `ConditionNodeConfig` semantics — `evaluator`, `elseTarget`,
# `requiresConfirmation`, `confirmationMessage`, plus the legacy
# `condition:` DSL migration. The `mode` discriminator lives at the
# top level; the `condition:` legacy string still migrates to
# `evaluator` exactly as before.
# ─────────────────────────────────────────────────────────────────
class TestBranchIfElseConfig:
    def test_default_evaluator_is_function_empty(self):
        """: `BranchNodeConfig` no longer carries
        the `ConditionNodeConfig` auto-flip safety net that turned an
        empty function-mode evaluator into a literal True. The
        default is `function` / empty expression — matching the prior
        `RouterNodeConfig` shape (also function-mode by default).
        Migrating `condition='always'` / `'never'` / `''` literals
        still flips to literal via `_migrate_legacy_condition`."""
        cfg = BranchNodeConfig.model_validate({"mode": "if-else"})
        assert cfg.evaluator.mode == "function"
        assert cfg.evaluator.expression == ""
        assert cfg.evaluator.migrated_from_legacy is False
        assert cfg.else_target == ""
        assert cfg.requires_confirmation is False
        assert cfg.confirmation_message == ""

    def test_else_target_alias(self):
        cfg = BranchNodeConfig.model_validate({"mode": "if-else", "elseTarget": "node-x"})
        assert cfg.else_target == "node-x"

    def test_requires_confirmation_alias(self):
        cfg = BranchNodeConfig.model_validate({
            "mode": "if-else",
            "requiresConfirmation": True,
            "confirmationMessage": "Continue?",
        })
        assert cfg.requires_confirmation is True
        assert cfg.confirmation_message == "Continue?"

    def test_legacy_dsl_contains_auto_migrates(self):
        """Old workflows carrying the `contains:` DSL are auto-migrated
        to `evaluator.mode='function'` on save."""
        cfg = BranchNodeConfig.model_validate({
            "mode": "if-else",
            "condition": "contains:urgent",
        })
        assert cfg.evaluator.mode == "function"
        assert "'urgent'" in cfg.evaluator.expression
        assert "previous_step_content" in cfg.evaluator.expression
        assert cfg.evaluator.migrated_from_legacy is True
        # Legacy field is dropped after migration — BranchNodeConfig
        # doesn't expose a `condition` field at all.
        assert "condition" not in cfg.model_dump()

    def test_legacy_dsl_equals_migrates(self):
        cfg = BranchNodeConfig.model_validate({
            "mode": "if-else",
            "condition": "equals:billing",
        })
        assert cfg.evaluator.mode == "function"
        assert "previous_step_content" in cfg.evaluator.expression
        assert "'billing'" in cfg.evaluator.expression
        assert cfg.evaluator.migrated_from_legacy is True

    def test_legacy_dsl_regex_migrates(self):
        cfg = BranchNodeConfig.model_validate({
            "mode": "if-else",
            "condition": "regex:^\\d+$",
        })
        assert cfg.evaluator.mode == "function"
        assert "search" in cfg.evaluator.expression
        assert "re" in cfg.evaluator.expression
        assert cfg.evaluator.migrated_from_legacy is True

    def test_legacy_always_migrates_to_literal_true(self):
        cfg = BranchNodeConfig.model_validate({
            "mode": "if-else",
            "condition": "always",
        })
        assert cfg.evaluator.mode == "literal"
        assert cfg.evaluator.expression == "True"

    def test_legacy_never_migrates_to_literal_false(self):
        cfg = BranchNodeConfig.model_validate({
            "mode": "if-else",
            "condition": "never",
        })
        assert cfg.evaluator.mode == "literal"
        assert cfg.evaluator.expression == "False"

    def test_bare_string_defaults_to_contains_migration(self):
        """A bare string (no `contains:` prefix) was always treated
        as `contains:<raw>` in the legacy DSL — preserve that."""
        cfg = BranchNodeConfig.model_validate({
            "mode": "if-else",
            "condition": "urgent",
        })
        assert cfg.evaluator.mode == "function"
        assert "'urgent'" in cfg.evaluator.expression
        assert cfg.evaluator.migrated_from_legacy is True

    def test_new_evaluator_wins_over_legacy(self):
        """If both `condition` and `evaluator.expression` are set,
        the new field wins and legacy is cleared (no migration flag)."""
        cfg = BranchNodeConfig.model_validate({
            "mode": "if-else",
            "condition": "contains:old",
            "evaluator": {"mode": "function", "expression": "new_expr"},
        })
        assert cfg.evaluator.expression == "new_expr"
        assert cfg.evaluator.migrated_from_legacy is False
        # Legacy field is dropped — only the evaluator survives.
        assert "condition" not in cfg.model_dump()

    def test_cel_evaluator_preserved(self):
        cfg = BranchNodeConfig.model_validate({
            "mode": "if-else",
            "evaluator": {"mode": "cel", "expression": "input.contains('urgent')"},
        })
        assert cfg.evaluator.mode == "cel"
        assert cfg.evaluator.expression == "input.contains('urgent')"
        assert cfg.evaluator.migrated_from_legacy is False

    def test_empty_function_evaluator_preserved(self):
        """: unlike the prior `ConditionNodeConfig`,
        `BranchNodeConfig` does NOT auto-flip empty function-mode
        expressions to a literal True. The default safety net was
        removed when the two config types collapsed — an empty
        function expression now reaches the emitter as-is, which
        surfaces the bug faster (compiler error at parse time)."""
        cfg = BranchNodeConfig.model_validate({
            "mode": "if-else",
            "evaluator": {"mode": "function", "expression": ""},
        })
        assert cfg.evaluator.mode == "function"
        assert cfg.evaluator.expression == ""

# ─────────────────────────────────────────────────────────────────
# Branch — switch mode (replaces TestRouterConfig after ,
#  — `router` + `condition` → `branch` collapse)
#
# The `branch` node type with `mode='switch'` keeps all the prior
# `RouterNodeConfig` semantics — `selector` (function/cel/hitl) +
# `branches`. The `mode` discriminator lives at the top level.
# ─────────────────────────────────────────────────────────────────
class TestBranchSwitchConfig:
    def test_empty(self):
        """Empty config seeds `mode='function'` (clean break — push
        toward deterministic, no implicit LLM fallback).
        No `condition` field, no migration logic."""
        cfg = BranchNodeConfig.model_validate({"mode": "switch"})
        assert cfg.selector.mode == "function"
        assert cfg.selector.expression == ""
        assert cfg.selector.fallback_message == ""
        assert cfg.branches == []

    def test_branches_well_formed(self):
        cfg = BranchNodeConfig.model_validate({
            "mode": "switch",
            "branches": [
                {"label": "FAQ", "target": "n1"},
                {"label": "Fallback", "target": "n2"},
            ],
        })
        assert len(cfg.branches) == 2
        assert cfg.branches[0].target == "n1"

    # ─── Branch rename — selector 3-mode ──────────────────────────
    def test_selector_function_mode_round_trip(self):
        """`function` mode: a Python expression that returns the chosen
        branch's step object."""
        cfg = BranchNodeConfig.model_validate({
            "mode": "switch",
            "selector": {
                "mode": "function",
                "expression": "'billing' in previous_step_content",
            },
        })
        assert cfg.selector.mode == "function"
        assert cfg.selector.expression == "'billing' in previous_step_content"

    def test_selector_cel_mode_round_trip(self):
        cfg = BranchNodeConfig.model_validate({
            "mode": "switch",
            "selector": {
                "mode": "cel",
                "expression": 'input.contains("video") ? "video_step" : "image_step"',
            },
        })
        assert cfg.selector.mode == "cel"
        assert "video_step" in cfg.selector.expression

    def test_selector_hitl_mode_round_trip(self):
        """`hitl` mode: pause and ask the user. `fallback_message` is
        the prompt shown when the user is asked to pick."""
        cfg = BranchNodeConfig.model_validate({
            "mode": "switch",
            "selector": {
                "mode": "hitl",
                "fallbackMessage": "Which department should handle this?",
            },
        })
        assert cfg.selector.mode == "hitl"
        assert cfg.selector.fallback_message == "Which department should handle this?"

    def test_unknown_selector_mode_rejected(self):
        """Pydantic Literal protects against typos — `'lllm'` would
        otherwise silently round-trip and confuse the emitter."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            BranchNodeConfig.model_validate({
                "mode": "switch",
                "selector": {"mode": "lllm"},
            })

    def test_legacy_condition_field_silently_ignored(self):
        """Clean break. The legacy top-level
        `condition` field is dropped (was unused at runtime anyway).
        Pydantic's extra="ignore" keeps the save from 422'ing, but
        the field is no longer mapped to anything."""
        cfg = BranchNodeConfig.model_validate({
            "mode": "switch",
            "condition": "old hint that nobody reads anymore",
            "selector": {"mode": "function", "expression": "na_step"},
        })
        # Legacy field is dropped — only the selector matters now
        assert not hasattr(cfg, "condition") or cfg.model_dump().get("condition", "") == ""
        assert cfg.selector.mode == "function"
        assert cfg.selector.expression == "na_step"

# ─────────────────────────────────────────────────────────────────
# F7 — `BranchTarget.condition` default → `Optional[str] = None`
# 
#
# The router picker is gone (see `strategies/router.py` §"Router
# LLM-free"), so the per-branch `condition` field is unused at
# runtime. Changing the default from "" to None signals "unused
# field" to readers; behaviour is identical (both are falsy in
# `if branch.condition:` checks).
# ─────────────────────────────────────────────────────────────────
class TestBranchTarget:
    def test_condition_default_is_none(self):
        """F7 — the per-branch `condition` field defaults to None,
        not "". Both are falsy but None clearly signals 'unset'."""
        from app.schemas.node_configs import BranchTarget
        bt = BranchTarget(label="yes", target="yes_agent")
        assert bt.condition is None

    def test_condition_can_still_be_set_explicitly(self):
        """Back-compat: callers that explicitly set it (legacy DSL
        migration callers) still get the value they pass."""
        from app.schemas.node_configs import BranchTarget
        bt = BranchTarget(label="yes", target="yes_agent", condition="contains:urgent")
        assert bt.condition == "contains:urgent"

    def test_branch_switch_config_default_branches_have_none_condition(self):
        """`BranchNodeConfig.branches` (switch mode) defaults to [];
        when populated without a `condition` key, each
        BranchTarget.condition is None."""
        from app.schemas.node_configs import BranchNodeConfig
        cfg = BranchNodeConfig(
            mode="switch",
            selector={"mode": "function", "expression": "x"},
            branches=[
                {"label": "yes", "target": "yes_agent"},
                {"label": "no",  "target": "no_agent"},
            ],
        )
        assert cfg.branches[0].condition is None
        assert cfg.branches[1].condition is None

# ─────────────────────────────────────────────────────────────────
# Dispatcher
# ─────────────────────────────────────────────────────────────────
class TestValidateNodeConfig:
    """The dispatcher is what `WorkflowNode`'s `model_validator` calls.
    Pin the contract: pass-through on unknown types / empty configs,
    typed validation on known types."""

    def test_known_type_validates(self):
        out = validate_node_config("loop", {"maxIterations": 5})
        assert hasattr(out, "model_dump")
        assert out.max_iterations == 5

    def test_unknown_type_passes_through(self):
        """`input` / `output` aren't real types anymore — the type
        union keeps them for legacy JSON imports. We must not 422 on
        a save that includes one."""
        cfg = {"legacy": True}
        out = validate_node_config("output", cfg)
        assert out is cfg

    def test_empty_config_passes_through(self):
        """Canvas mid-edit: an empty `data` shouldn't fail validation
        — the user might be wiring edges first. An empty dict gets
        normalized into a fully-typed model with all defaults, which
        is what downstream consumers expect."""
        out = validate_node_config("agent", {})
        # Typed object with all defaults, not a raw dict
        assert hasattr(out, "model_dump")
        assert out.model is None  # agent: default = no model → use preset fallback
        assert out.instructions == ""
        # None / non-dict pass through unchanged (defensive)
        assert validate_node_config("agent", None) is None
        assert validate_node_config("agent", "not a dict") == "not a dict"

# ─────────────────────────────────────────────────────────────────
# End-to-end through POST /workflows
# ─────────────────────────────────────────────────────────────────
class TestWorkflowEndpointRejectsBadConfig:
    def test_max_iterations_too_large(self, client):
        r = client.post(
            "/api/v1/workflows",
            json={
                "name": "Bad",
                "nodes": [{
                    "id": "l", "type": "loop", "position": {"x": 0, "y": 0},
                    "data": {"config": {"maxIterations": 99999}},
                }],
                "edges": [],
            },
        )
        assert r.status_code == 422
        body = r.json()
        # FastAPI / Pydantic put the per-field error under `detail`.
        assert "detail" in body

    def test_unknown_method_rejected(self, client):
        r = client.post(
            "/api/v1/workflows",
            json={
                "name": "Bad",
                "nodes": [{
                    "id": "h", "type": "tool", "position": {"x": 0, "y": 0},
                    "data": {"config": {"source": "http", "method": "DELETE"}},
                }],
                "edges": [],
            },
        )
        assert r.status_code == 422

    def test_empty_data_still_saves(self, client):
        """Empty `data` is mid-edit state — should save (201), not
        422. The runtime's default-preset fallback handles the
        no-model case (see BUG_FIX)."""
        r = client.post(
            "/api/v1/workflows",
            json={
                "name": "Empty agent",
                "nodes": [{
                    "id": "a", "type": "agent", "position": {"x": 0, "y": 0},
                    "data": {},
                }],
                "edges": [],
            },
        )
        assert r.status_code == 201

    def test_unknown_node_type_still_422(self, client):
        """The pre-existing `NodeType` Literal still rejects unknown
        types at the parent level. This is unchanged."""
        r = client.post(
            "/api/v1/workflows",
            json={
                "name": "Bad",
                "nodes": [{
                    "id": "x", "type": "wat", "position": {"x": 0, "y": 0},
                    "data": {},
                }],
                "edges": [],
            },
        )
        assert r.status_code == 422

# ─────────────────────────────────────────────────────────────────
# J — every per-node-config field carries a `Field(description=)`
# so the chat-builder LLM tool (`get_node_types`) can surface useful
# per-field docs. Without the description the LLM sees an empty string
# and falls back to asking follow-up questions.
# ─────────────────────────────────────────────────────────────────
class TestSchemaFieldDescriptions:
    """Pin: every field on every per-node-config BaseModel has a
    non-empty `description`. New fields must add their own — the
    reminder lives in the `Field(...)` factory, so dropping a
    description is a visible diff.

    The exhaustive list lives in `node_configs.py`; this test
    iterates the schema classes so it stays in sync automatically
    when a new schema class or field is added. Failing this test
    means the LLM gets a blank `description` for that field — fix
    by adding `description="..."` to the `Field(...)` call.
    """

    # Each entry is a concrete BaseModel subclass whose fields the
    # LLM should be able to introspect via `get_node_types`. Nested
    # types (e.g. `BranchTarget` inside `BranchNodeConfig.branches`)
    # are listed explicitly because `model_fields` is per-class —
    # nested types don't appear in the parent's fields.
    # McpNodeConfig + HttpNodeConfig + ToolsNodeConfig collapsed
    # into ToolNodeConfig. Only the merged type stays on the
    # docs list.
    SCHEMAS_WITH_DOCS = [
        ModelConfig,
        AgentNodeConfig,
        ToolNodeConfig,
        BranchNodeConfig,
        LoopNodeConfig,
        from_app_schemas("ParamSchema"),
        from_app_schemas("ToolFunction"),
        from_app_schemas("BranchTarget"),
        from_app_schemas("RouterSelector"),
        from_app_schemas("FlowNodeConfig"),
        from_app_schemas("ConditionEvaluator"),
        from_app_schemas("AskConfig"),
    ]

    def test_every_field_has_a_description(self):
        missing: list[str] = []
        for model in self.SCHEMAS_WITH_DOCS:
            for name, info in model.model_fields.items():
                if not (info.description or "").strip():
                    missing.append(f"{model.__name__}.{name}")
        assert not missing, (
            "these fields are missing `Field(description=...)` — the "
            "chat-builder LLM gets blank docs for them: "
            + ", ".join(sorted(missing))
        )

    def test_descriptions_are_short_enough_for_system_prompt(
        self,
    ):
        """Defensive: a runaway description would bloat the system
        prompt if the LLM tool ever inlines the fields. Cap each at
        400 chars (~80 tokens) so even an inlined rendering stays
        within budget."""
        offenders: list[tuple[str, str, int]] = []
        for model in self.SCHEMAS_WITH_DOCS:
            for name, info in model.model_fields.items():
                desc = info.description or ""
                if len(desc) > 400:
                    offenders.append(
                        (model.__name__, name, len(desc))
                    )
        assert not offenders, (
            "these descriptions exceed 400 chars and would bloat the "
            "system prompt if inlined: "
            + ", ".join(
                f"{cls}.{name} ({n} chars)"
                for cls, name, n in offenders
            )
        )