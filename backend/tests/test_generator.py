"""Tests for the standalone Python code generator.

The generator turns a workflow JSON (nodes + edges) into a single Python file
that depends ONLY on `agno` (plus stdlib + optional `requests`). The tests here
focus on the *output text* — we assert on substrings rather than executing the
generated code, because:
- we don't want to actually call LLMs in CI
- generated code may reference user-specific tool bodies

NOTE: the platform has 6 base node types (no `input`/`output` —
those map to `Workflow.run(input=...)` and the last Step's result).
"""
from __future__ import annotations

import ast
import re

import pytest

from app.core.compile import CompileError as GeneratorError, to_python_source as render_python

import ast as _ast
import importlib.util as _importlib_util
import sys as _sys
import types as _types
from pathlib import Path as _Path

# Repo root so we can find /examples after the generator dumps them.
_REPO_ROOT = _Path(__file__).resolve().parents[2]
_EXAMPLES_DIR = _REPO_ROOT / "examples"
_DUMP_SCRIPT = _Path(__file__).resolve().parents[1] / "scripts" / "dump_examples.py"

# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────
def _minimal_workflow():
    """Single agent (openai/gpt-4o), no edges."""
    return {
        "name": "hello",
        "nodes": [
            {"id": "n2", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "Greeter",
                      "config": {
                          "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "sk-test"},
                          "instructions": "Say hello",
                      }}},
        ],
        "edges": [],
    }

def _parse(code: str):
    """Verify the generated code is syntactically valid Python."""
    return ast.parse(code)

# ─────────────────────────────────────────────────────────────────
# Minimal workflow → valid Python file
# ─────────────────────────────────────────────────────────────────
def test_generate_minimal_workflow_returns_string():
    result = render_python(_minimal_workflow())
    assert isinstance(result, str)
    assert len(result) > 100

def test_generated_code_is_syntactically_valid():
    result = render_python(_minimal_workflow())
    tree = _parse(result)  # raises SyntaxError on failure
    assert tree is not None

def test_generated_code_only_imports_agno():
    """The exported code must not import any module from this platform."""
    result = render_python(_minimal_workflow())
    tree = _parse(result)
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                imported.append(n.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.append(node.module)
    # All agno.* imports are allowed; nothing from app.* should leak.
    assert any(m == "agno" or m.startswith("agno.") for m in imported), \
        f"expected at least one agno import, got {imported}"
    assert not any(m.startswith("app.") or m == "app" for m in imported), \
        f"generator must not emit 'app.*' imports, got {imported}"

def test_generated_code_has_main_block():
    result = render_python(_minimal_workflow())
    assert "if __name__" in result
    assert 'workflow.run(' in result or 'workflow.print_response(' in result

def test_render_python_helper_matches_generate():
    """render_python is the underlying pure-Python renderer (no DB)."""
    code = render_python(_minimal_workflow())
    assert "from agno" in code
    assert "def main" in code or "if __name__" in code

def test_generate_raises_on_empty_workflow():
    with pytest.raises(GeneratorError):
        render_python({"name": "x", "nodes": [], "edges": []})

def test_generate_raises_on_cycle():
    """A cycle (without break-out) trips topo-sort, surface as GeneratorError."""
    wf = {
        "name": "cycle",
        "nodes": [
            {"id": "n2", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A",
                      "config": {"model": {"provider": "openai", "modelId": "x", "apiKey": "k"},
                                 "instructions": "x"}}},
            {"id": "n3", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "B",
                      "config": {"model": {"provider": "openai", "modelId": "x", "apiKey": "k"},
                                 "instructions": "x"}}},
        ],
        "edges": [
            {"id": "e1", "source": "n2", "target": "n3"},
            {"id": "e2", "source": "n3", "target": "n2"},
        ],
    }
    with pytest.raises(GeneratorError, match=r"(cycle|loop)"):
        render_python(wf)

def test_generate_raises_on_unknown_node_type():
    wf = _minimal_workflow()
    wf["nodes"].append({"id": "nbad", "type": "alien", "position": {"x": 0, "y": 0},
                        "data": {"label": "x", "config": {}}})
    with pytest.raises(GeneratorError):
        render_python(wf)

# ─────────────────────────────────────────────────────────────────
# Agent provider branching
# ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("provider,expected_import", [
    ("openai", "from agno.models.openai import OpenAIChat"),
    ("anthropic", "from agno.models.anthropic import Claude"),
    ("ollama", "from agno.models.ollama import Ollama"),
    ("google", "from agno.models.google import Gemini"),
])
def test_agent_provider_emits_correct_model_import(provider, expected_import):
    wf = _minimal_workflow()
    wf["nodes"][0]["data"]["config"]["model"] = {
        "provider": provider, "modelId": "some-model", "apiKey": "k",
    }
    code = render_python(wf)
    assert expected_import in code, f"missing {expected_import!r} in:\n{code[:600]}"

def test_agent_writes_api_key_into_code():
    wf = _minimal_workflow()
    code = render_python(wf)
    # v1: api key hardcoded into exported code
    assert "sk-test" in code

def test_agent_instructions_passed_through():
    wf = _minimal_workflow()
    wf["nodes"][0]["data"]["config"]["instructions"] = "You are a pirate."
    code = render_python(wf)
    assert "You are a pirate." in code

# ─────────────────────────────────────────────────────────────────
# Agent field extensions
# ─────────────────────────────────────────────────────────────────
def test_agent_omits_phase91_kwargs_by_default():
    """With no extended-agent fields set, the generated Agent(...)
    stays compact (no `system_message=""` noise). This keeps
    pre-extension export snapshots byte-identical for users who
    don't opt in.
    """
    code = render_python(_minimal_workflow())
    assert "system_message" not in code
    assert "reasoning" not in code
    assert "retries" not in code
    assert "tool_call_limit" not in code
    assert "add_datetime_to_context" not in code
    assert "parser_model" not in code
    assert "pre_hooks" not in code
    assert "post_hooks" not in code

def test_agent_emits_system_message_and_reasoning():
    """systemMessage + reasoning + reasoningModel flow through to
    the constructor kwargs verbatim."""
    wf = _minimal_workflow()
    cfg = wf["nodes"][0]["data"]["config"]
    cfg["systemMessage"] = "Be terse."
    cfg["reasoning"] = True
    cfg["reasoningModel"] = {"provider": "openai", "modelId": "o1-mini"}
    code = render_python(wf)
    assert 'system_message="Be terse.",' in code
    assert "reasoning=True," in code
    assert "reasoning_model=" in code
    assert "o1-mini" in code

def test_agent_emits_retries_and_delay_and_tool_call_limit():
    """retries + delayBetweenRetries + toolCallLimit all flow through."""
    wf = _minimal_workflow()
    cfg = wf["nodes"][0]["data"]["config"]
    cfg["retries"] = 3
    cfg["delayBetweenRetries"] = 5
    cfg["toolCallLimit"] = 10
    code = render_python(wf)
    assert "retries=3," in code
    assert "delay_between_retries=5," in code
    assert "tool_call_limit=10," in code

def test_agent_emits_add_datetime_to_context_bool():
    """addDatetimeToContext is a true/false flag — only emits True."""
    wf = _minimal_workflow()
    wf["nodes"][0]["data"]["config"]["addDatetimeToContext"] = True
    code = render_python(wf)
    assert "add_datetime_to_context=True," in code

def test_agent_emits_parser_model_and_prompt():
    """parserModel + parserModelPrompt emit as a pair."""
    wf = _minimal_workflow()
    cfg = wf["nodes"][0]["data"]["config"]
    cfg["parserModel"] = {"provider": "openai", "modelId": "gpt-4o-mini"}
    cfg["parserModelPrompt"] = "Return JSON."
    code = render_python(wf)
    assert "parser_model=" in code
    assert "gpt-4o-mini" in code
    assert 'parser_model_prompt="Return JSON.",' in code

def test_agent_emits_hooks_referencing_tools_node_function_names():
    """preHooks / postHooks render as `pre_hooks=[func1, func2]` —
    plain function names, not `Function.from_callable(...)` wrappers.

    The old `tools` node type is now `tool`
    with `source='function'`."""
    wf = {
        "name": "hooks-demo",
        "nodes": [
            {"id": "hooks", "type": "tool", "position": {"x": 0, "y": 0},
             "data": {"label": "MyHooks", "config": {"source": "function", 
                 "functions": [
                     {"name": "audit_log", "code": "def audit_log(msg):\n    return None\n"},
                     {"name": "enrich", "code": "def enrich():\n    return None\n"},
                 ],
             }}},
            {"id": "na", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A", "config": {
                 "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                 "instructions": "do",
                 "preHooks": ["hooks"],
                 "postHooks": ["hooks"],
             }}},
        ],
        "edges": [],
    }
    code = render_python(wf)
    # pre_hooks + post_hooks both reference the function names
    assert "pre_hooks=[audit_log, enrich]," in code
    assert "post_hooks=[audit_log, enrich]," in code
    # function defs are present (so the names resolve)
    assert "def audit_log" in code
    assert "def enrich" in code

def test_agent_phase91_assembles_into_agno_agent():
    """Emitted code is exec-able and the resulting Agent has the
    extended fields wired through."""
    import types
    from agno.agent import Agent

    wf = _minimal_workflow()
    cfg = wf["nodes"][0]["data"]["config"]
    cfg["systemMessage"] = "Be terse."
    cfg["reasoning"] = True
    cfg["retries"] = 2
    cfg["delayBetweenRetries"] = 3
    cfg["toolCallLimit"] = 7
    cfg["addDatetimeToContext"] = True
    code = render_python(wf)
    mod = types.ModuleType("agb_agent_phase91_exec")
    exec(compile(code, "<agent-phase91-test>", "exec"), mod.__dict__)
    agent = mod.n2_agent
    assert isinstance(agent, Agent)
    assert agent.system_message == "Be terse."
    assert agent.reasoning is True
    assert agent.retries == 2
    assert agent.delay_between_retries == 3
    assert agent.tool_call_limit == 7
    assert agent.add_datetime_to_context is True

# ─────────────────────────────────────────────────────────────────
# Human Input node
# ─────────────────────────────────────────────────────────────────
def test_human_input_text_emits_input_call():
    """human_input nodes emit a `Step(requires_user_input=True)`
    via the agno-native pause/resume protocol — the legacy
    `def ask_<nid>`-then-stdin-`input()` helper is gone (it predated
    the single-engine refactor). The runtime pause UI is the only
    path the frontend knows how to render a confirmation; emitting a
    bare `input()` call would deadlock headless runs.
    """
    wf = _minimal_workflow()
    # insert a human_input between agent and output
    wf["nodes"].insert(1, {
        "id": "nh", "type": "human_input", "position": {"x": 0, "y": 0},
        "data": {"label": "Ask", "config": {"prompt": "Continue? (y/n)",
                                              "inputType": "confirm"}},
    })
    code = render_python(wf)
    assert "requires_user_input=True" in code
    assert "user_input_message=" in code
    assert "Continue?" in code  # prompt text preserved

# ─────────────────────────────────────────────────────────────────
# Tools node
# ─────────────────────────────────────────────────────────────────
def test_tools_node_emits_function_definitions():
    wf = {
        "name": "tools-demo",
        "nodes": [
            {"id": "nt", "type": "tool", "position": {"x": 0, "y": 0},
             "data": {"label": "MyTools", "config": {"source": "function", 
                 "functions": [
                     {"name": "add", "description": "Add two numbers",
                      "parameters": [
                          {"name": "a", "type": "number", "required": True},
                          {"name": "b", "type": "number", "required": True},
                      ],
                      "code": "def add(a, b):\n    return a + b\n"},
                 ],
             }}},
            {"id": "na", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "CalcAgent",
                      "config": {
                          "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                          "instructions": "Use the add tool.",
                          "toolsRef": ["nt"],
                      }}},
        ],
        # `tools` nodes are NOT wired by edges — they're referenced via
        # `agent.cfg.toolsRef`. The agent is the sole executable node.
        "edges": [],
    }
    code = render_python(wf)
    assert "def add(a, b)" in code or "def add(" in code
    assert "from agno.tools.function import Function" in code or "Function.from_callable" in code
    # Tools are wired via `nid_agent.tools = [...]` after the Agent object.
    assert "na_agent.tools = [" in code
    assert "Function.from_callable(add" in code

# ─────────────────────────────────────────────────────────────────
# MCP node
# ─────────────────────────────────────────────────────────────────
def test_mcp_node_emits_mcp_tools_construction():
    wf = {
        "name": "mcp-demo",
        "nodes": [
            {"id": "nm", "type": "tool", "position": {"x": 0, "y": 0},
             "data": {"label": "FS", "config": {"source": "mcp", 
                 "serverId": "mcp-fs",
                 "toolNamePrefix": "fs_",
             }}},
            {"id": "na", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "FileAgent",
                      "config": {
                          "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                          "instructions": "Read files.",
                          "toolsRef": ["nm"],
                      }}},
        ],
        # mcp is a tool-source node — wired via toolsRef, not edges.
        "edges": [],
    }
    code = render_python(wf)
    assert "MCPTools" in code
    assert "command=" in code  # stdio mode passes command
    # comment tells user to start the MCP server themselves
    assert "MCP" in code and ("# " in code or '"""' in code)

# ─────────────────────────────────────────────────────────────────
# HTTP node
# ─────────────────────────────────────────────────────────────────
def test_tool_node_emits_http_wrapper_function_with_requests():
    """The old `http` node type is now
    `tool` with `source='http'`. The generator emits the same
    HTTP wrapper function shape — byte-stable for source='http'."""
    wf = {
        "name": "http-demo",
        "nodes": [
            {"id": "nh", "type": "tool", "position": {"x": 0, "y": 0},
             "data": {"label": "GetUser", "config": {"source": "http", 
                 "toolName": "fetch_user",
                 "toolDescription": "Look up a user by id",
                 "baseUrl": "https://api.example.com",
                 "method": "GET",
                 "path": "/users/{user_id}",
                 "authToken": "TOKEN-XYZ",
             }}},
            {"id": "na", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "HTTPAgent",
                      "config": {
                          "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                          "instructions": "Call fetch_user.",
                          "toolsRef": ["nh"],
                      }}},
        ],
        "edges": [],
    }
    code = render_python(wf)
    assert "import requests" in code
    assert "def fetch_user" in code
    assert "TOKEN-XYZ" in code
    assert "requests.get" in code or "requests.post" in code
    assert "{user_id}" in code or "user_id" in code  # path template preserved

# ─────────────────────────────────────────────────────────────────
# Router node
# ─────────────────────────────────────────────────────────────────
def test_router_node_emits_router_construction():
    """Clean break: the legacy `condition` field
    is gone. New shape: `selector.mode='function'` + `expression`."""
    wf = {
        "name": "router-demo",
        "nodes": [
            {"id": "nr", "type": "router", "position": {"x": 0, "y": 0},
             "data": {"label": "Route", "config": {
                 "selector": {
                     "mode": "function",
                     "expression": "na_long_step if len(input) > 10 else None",
                 },
                 "branches": [
                     {"label": "long", "target": "na_long"},
                 ],
             }}},
            {"id": "na_long", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "Long",
                      "config": {"model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                                 "instructions": "say long"}}},
        ],
        "edges": [
            {"id": "e2", "source": "nr", "target": "na_long"},
        ],
    }
    code = render_python(wf)
    assert "Router(" in code
    assert "selector" in code

# ─────────────────────────────────────────────────────────────────
# Flow node — `mode='parallel'`
# ─────────────────────────────────────────────────────────────────
def test_parallel_node_emits_parallel_construction():
    wf = {
        "name": "parallel-demo",
        "nodes": [
            {"id": "np", "type": "flow", "position": {"x": 0, "y": 0},
             "data": {"label": "FanOut", "config": {
                 "mode": "parallel",
                 "branches": [
                     {"label": "a", "target": "na_a"},
                     {"label": "b", "target": "na_b"},
                 ],
             }}},
            {"id": "na_a", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A",
                      "config": {"model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                                 "instructions": "say A"}}},
            {"id": "na_b", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "B",
                      "config": {"model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                                 "instructions": "say B"}}},
        ],
        "edges": [
            {"id": "e2", "source": "np", "target": "na_a"},
            {"id": "e3", "source": "np", "target": "na_b"},
        ],
    }
    code = render_python(wf)
    assert "Parallel(" in code

# ─────────────────────────────────────────────────────────────────
# Steps node
# ─────────────────────────────────────────────────────────────────
def test_steps_node_emits_steps_construction():
    """flow with `mode='sequential'` produces `Steps(...)`
    with step lists in edge order. Mirrors
    `test_parallel_node_emits_parallel_construction`."""
    wf = {
        "name": "steps-demo",
        "nodes": [
            {"id": "ns", "type": "flow", "position": {"x": 0, "y": 0},
             "data": {"label": "Pipeline", "config": {"mode": "sequential"}}},
            {"id": "ns_a", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A",
                      "config": {"model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                                 "instructions": "Step A"}}},
            {"id": "ns_b", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "B",
                      "config": {"model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                                 "instructions": "Step B"}}},
        ],
        "edges": [
            {"id": "e1", "source": "ns", "target": "ns_a"},
            {"id": "e2", "source": "ns", "target": "ns_b"},
        ],
    }
    code = render_python(wf)
    assert "from agno.workflow.steps import Steps" in code
    assert "ns_steps = Steps(" in code
    # Both branches inlined as `Step(name=..., agent=...)` since they're agents
    # (generator's `q()` uses double quotes — match that, don't be brittle)
    assert 'Step(name="A", agent=ns_a_agent)' in code
    assert 'Step(name="B", agent=ns_b_agent)' in code
    assert 'name="Pipeline"' in code

def test_steps_node_omits_hitl_kwargs_by_default():
    """Without `requiresConfirmation`, the generated `Steps(...)`
    stays compact — no `requires_confirmation=False` noise. Keeps
    pre-extension snapshots stable for users who don't opt in."""
    wf = {
        "name": "steps-default",
        "nodes": [
            {"id": "ns", "type": "flow", "position": {"x": 0, "y": 0},
             "data": {"label": "Pipeline", "config": {"mode": "sequential"}}},
            {"id": "ns_a", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A",
                      "config": {"model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                                 "instructions": "a"}}},
        ],
        "edges": [
            {"id": "e1", "source": "ns", "target": "ns_a"},
        ],
    }
    code = render_python(wf)
    assert "requires_confirmation" not in code
    assert "confirmation_message" not in code

def test_steps_node_emits_requires_confirmation_and_message():
    """: in `mode='sequential'`, `requiresConfirmation=true`
    + `confirmationMessage` flow through to the `Steps(...)`
    constructor kwargs verbatim."""
    wf = {
        "name": "steps-hitl",
        "nodes": [
            {"id": "ns", "type": "flow", "position": {"x": 0, "y": 0},
             "data": {"label": "Pipeline", "config": {
                 "mode": "sequential",
                 "requiresConfirmation": True,
                 "confirmationMessage": "Run the chain?",
             }}},
            {"id": "ns_a", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A",
                      "config": {"model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                                 "instructions": "a"}}},
        ],
        "edges": [
            {"id": "e1", "source": "ns", "target": "ns_a"},
        ],
    }
    code = render_python(wf)
    assert "requires_confirmation=True," in code
    assert 'confirmation_message="Run the chain?",' in code

def test_steps_node_assembles_into_agno_steps():
    """: emitted code is exec-able and the resulting Steps object
    has the HITL fields wired to the matching HumanReview fields."""
    import types
    from agno.workflow.steps import Steps

    wf = {
        "name": "steps-exec",
        "nodes": [
            {"id": "ns", "type": "flow", "position": {"x": 0, "y": 0},
             "data": {"label": "Pipeline", "config": {
                 "mode": "sequential",
                 "requiresConfirmation": True,
                 "confirmationMessage": "OK?",
             }}},
            {"id": "ns_a", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A",
                      "config": {"model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                                 "instructions": "a"}}},
        ],
        "edges": [
            {"id": "e1", "source": "ns", "target": "ns_a"},
        ],
    }
    code = render_python(wf)
    mod = types.ModuleType("agb_steps_exec")
    exec(compile(code, "<steps-test>", "exec"), mod.__dict__)
    assert isinstance(mod.ns_steps, Steps)
    assert mod.ns_steps.requires_confirmation is True
    assert mod.ns_steps.confirmation_message == "OK?"
    # Branch inlining: the Step wrapper is exposed as `ns_a_step`, not top-level.
    step_names = [type(s).__name__ for s in mod.workflow.steps]
    assert step_names.count("Steps") == 1
    assert step_names.count("Step") == 0, \
        f"branches should NOT be top-level steps, got {step_names}"

# ─────────────────────────────────────────────────────────────────
# Explicit Output node labelling
# ─────────────────────────────────────────────────────────────────
def test_output_node_renders_final_run():
    """The generated module's main() runs the workflow via agno's standard
    `Workflow.print_response(...)` API. (No literal `output` node required —
    the workflow's output is the last Step's result.)"""
    wf = _minimal_workflow()
    code = render_python(wf)
    assert "workflow.print_response" in code or "workflow.run(" in code

# ─────────────────────────────────────────────────────────────────
# Per-node-type coverage — one parametric test that builds a workflow
# containing every node type and asserts on the emitted structure.
# ─────────────────────────────────────────────────────────────────
def test_all_base_node_types_in_one_workflow():
    """All base node types: tool → agent(uses tool+http+function)
    → branch → flow → ask. Confirms the generator wires every
    node type without import or wiring regressions."""
    wf = {
        "name": "all_types",
        "nodes": [
            {"id": "nt", "type": "tool", "position": {"x": 0, "y": 0},
             "data": {"label": "MyTools", "config": {"source": "function", 
                 "functions": [{"name": "add", "description": "add",
                                "parameters": [], "code": "def add():\n    return 1\n"}],
             }}},
            {"id": "nh", "type": "human_input", "position": {"x": 0, "y": 0},
             "data": {"label": "Ask", "config": {"prompt": "OK?", "inputType": "text"}}},
            {"id": "nhttp", "type": "tool", "position": {"x": 0, "y": 0},
             "data": {"label": "GetUser", "config": {"source": "http", 
                 "toolName": "fetch_user", "toolDescription": "lookup",
                 "baseUrl": "https://api.example.com", "method": "GET",
                 "path": "/users/{uid}",
             }}},
            {"id": "na", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "Greeter", "config": {
                 "model": {"provider": "anthropic", "modelId": "claude-sonnet-4-5", "apiKey": "k"},
                 "instructions": "hi",
                 "toolsRef": ["nt", "nh", "nhttp"],
             }}},
            {"id": "nr", "type": "router", "position": {"x": 0, "y": 0},
             "data": {"label": "Route", "config": {
                 "condition": "True",
                 "branches": [{"target": "np_a"}, {"target": "np_b"}],
             }}},
            {"id": "np_a", "type": "flow", "position": {"x": 0, "y": 0},
             "data": {"label": "FanA", "config": {
                 "mode": "parallel",
                 "branches": [{"target": "na_a1"}, {"target": "na_a2"}],
             }}},
            {"id": "np_b", "type": "flow", "position": {"x": 0, "y": 0},
             "data": {"label": "FanB", "config": {
                 "mode": "parallel",
                 "branches": [{"target": "na_b1"}],
             }}},
            {"id": "na_a1", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A1", "config": {
                 "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                 "instructions": "a1"}}},
            {"id": "na_a2", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A2", "config": {
                 "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                 "instructions": "a2"}}},
            {"id": "na_b1", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "B1", "config": {
                 "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                 "instructions": "b1"}}},
            # MCP omitted from this test because it requires `pip install mcp`;
            # MCP is covered separately by test_mcp_node_emits_mcp_tools_construction.
        ],
        "edges": [
            {"id": "e5", "source": "na", "target": "nr"},
            {"id": "e6", "source": "nr", "target": "np_a"},
            {"id": "e7", "source": "nr", "target": "np_b"},
            {"id": "e8", "source": "np_a", "target": "na_a1"},
            {"id": "e9", "source": "np_a", "target": "na_a2"},
            {"id": "e10", "source": "np_b", "target": "na_b1"},
        ],
    }
    code = render_python(wf)

    # Every node type appears as an object definition:
    # The export uses `<nid>_step` (Step wrappers) and
    # `<nid>_agent` (Agent bodies) as the canonical variable names; the
    # legacy `ask_nh` helper is gone (human_input is now an agno-native
    # Step(requires_user_input=True)).
    assert "nh_step = Step(" in code  # human_input step
    assert "na_agent = Agent(" in code  # agent
    assert "nr_router" in code or "Router(" in code  # router
    assert "np_a_parallel" in code  # parallel
    assert "na_a1_step" in code  # agent step
    # Imports — every kind present in the workflow is imported:
    assert "from agno.models.openai import OpenAIChat" in code
    assert "from agno.models.anthropic import Claude" in code
    assert "from agno.workflow.router import Router" in code
    assert "from agno.workflow.parallel import Parallel" in code
    assert "from agno.tools.function import Function" in code
    # The whole module is syntactically valid Python:
    import ast as _ast
    _ast.parse(code)

def test_generated_workflow_module_executes():
    """The generated module must be loadable end-to-end (no NameErrors,
    no missing imports for used types). Uses a no-MCP shape so the test
    doesn't depend on the optional `mcp` package."""
    import types
    wf = {
        "name": "exec_test",
        "nodes": [
            {"id": "nt", "type": "tool", "position": {"x": 0, "y": 0},
             "data": {"label": "T", "config": {"source": "function", 
                 "functions": [{"name": "add", "description": "add",
                                "parameters": [], "code": "def add(a, b):\n    return a + b\n"}],
             }}},
            {"id": "nh", "type": "human_input", "position": {"x": 0, "y": 0},
             "data": {"label": "Ask", "config": {"prompt": "OK?", "inputType": "confirm"}}},
            {"id": "na", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A", "config": {
                 "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                 "instructions": "use tools",
                 "toolsRef": ["nt", "nh"],
             }}},
        ],
        "edges": [],
    }
    code = render_python(wf)
    mod = types.ModuleType("agb_test_exec")
    exec(code, mod.__dict__)
    assert mod.workflow.name == "exec_test"
    # 2 steps: the agent + the human_input (the human_input is an
    # executable node type — it shows up as its own Step regardless of
    # whether the agent references it via toolsRef).
    assert len(mod._steps) == 2
    assert mod.na_agent.tools  # tools wired
    assert mod.nh_step  # human_input Step exists

def test_human_input_emits_typed_helper():
    """All three human_input kinds (text/confirm/choice) emit the
    agno-native `Step(requires_user_input=True, ...)` with the
    matching `user_input_schema`.

    The legacy `ask_nh(text: str) -> str`-style helper
    is gone — the runtime pause UI is the only path the frontend
    knows how to render. The compiled `Step` carries the schema field
    type so agno's pause protocol knows what to render.
    """
    expected_field = {"text": "response", "confirm": "confirmation", "choice": "selection"}
    for kind in ["text", "confirm", "choice"]:
        wf = {
            "name": f"hi_{kind}",
            "nodes": [
                {"id": "nh", "type": "human_input", "position": {"x": 0, "y": 0},
                 "data": {"label": "Ask", "config": {
                     "prompt": "Q?", "inputType": kind,
                     **({"choices": ["a", "b"]} if kind == "choice" else {}),
                 }}},
            ],
            "edges": [],
        }
        code = render_python(wf)
        assert "nh_step = Step(" in code
        assert "requires_user_input=True" in code
        assert expected_field[kind] in code, f"{kind} kind missing field {expected_field[kind]!r}"

# ─────────────────────────────────────────────────────────────────
# Branch rename — 3-mode selector (no LLM picker)
# ─────────────────────────────────────────────────────────────────
def _router_two_branches_wf(selector: dict) -> dict:
    """Helper: minimal workflow with a router (selector) → 2 agents."""
    return {
        "name": "router-modes",
        "nodes": [
            {"id": "nr", "type": "router", "position": {"x": 0, "y": 0},
             "data": {"label": "R", "config": {"selector": selector}}},
            {"id": "na_faq", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "FAQ",
                      "config": {"model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                                 "instructions": "faq"}}},
            {"id": "na_support", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "Support",
                      "config": {"model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                                 "instructions": "support"}}},
        ],
        "edges": [
            {"id": "e1", "source": "nr", "target": "na_faq"},
            {"id": "e2", "source": "nr", "target": "na_support"},
        ],
    }

def test_router_function_mode_emits_callable_selector():
    """`function` mode: emit a Python function that returns a step."""
    code = render_python(_router_two_branches_wf({
        "mode": "function",
        "expression": "na_faq_step if 'how' in previous_step_content else na_support_step",
    }))
    assert "def nr_selector(step_input):" in code
    assert "previous_step_content" in code
    assert "session_state" in code
    assert "selector=nr_selector" in code
    # Both branches visible in the choices list — emitted in edge order
    assert "choices=[na_faq_step, na_support_step]" in code

def test_router_function_mode_emitted_code_is_exec_able():
    """Generated `def nr_selector(step_input)` is executable Python and
    returns the chosen branch's step object."""
    import types
    from agno.workflow.router import Router

    code = render_python(_router_two_branches_wf({
        "mode": "function",
        "expression": "na_faq_step",
    }))
    mod = types.ModuleType("agb_router_func_test")
    exec(compile(code, "<router-func-test>", "exec"), mod.__dict__)
    assert isinstance(mod.nr_router, Router)
    assert callable(mod.nr_router.selector)
    # Run the selector with a mock step_input
    class _MockSI:
        previous_step_content = None
        previous_step_outputs = {}
        input = ""
        additional_data = {}
        session_state = {}
    result = mod.nr_router.selector(_MockSI())
    assert result is mod.na_faq_step

def test_router_cel_mode_emits_selector_string():
    """`cel` mode: pass the expression as a quoted string to Router."""
    code = render_python(_router_two_branches_wf({
        "mode": "cel",
        "expression": 'input.contains("billing") ? "na_faq_step" : "na_support_step"',
    }))
    assert 'selector="input.contains' in code or "selector='input.contains" in code
    # No `def nr_selector` for CEL — it's the string itself
    assert "def nr_selector" not in code

def test_router_hitl_mode_emits_requires_user_input_and_message():
    """`hitl` mode: emit `Router(requires_user_input=True,
    user_input_message=<fallback_message>)` — no selector callable."""
    code = render_python(_router_two_branches_wf({
        "mode": "hitl",
        "fallbackMessage": "Pick a department:",
    }))
    assert "requires_user_input=True" in code
    assert 'user_input_message="Pick a department:"' in code or "user_input_message='Pick a department:'" in code
    # No selector emitted for HITL — agno handles the choice
    assert "selector=" not in code

def test_router_hitl_mode_emitted_router_is_hitl():
    """End-to-end: the generated Router has requires_user_input set +
    user_input_message carried through."""
    import types
    from agno.workflow.router import Router

    code = render_python(_router_two_branches_wf({
        "mode": "hitl",
        "fallbackMessage": "Pick one:",
    }))
    mod = types.ModuleType("agb_router_hitl_test")
    exec(compile(code, "<router-hitl-test>", "exec"), mod.__dict__)
    assert isinstance(mod.nr_router, Router)
    assert mod.nr_router.requires_user_input is True
    assert mod.nr_router.user_input_message == "Pick one:"
    assert len(mod.nr_router.choices) == 2

def test_filename_sanitization():
    """Workflow name with unsafe chars produces a safe Python filename."""
    from app.core.compile._helpers.utils import safe_name
    assert safe_name("My Flow 2!") == "my_flow_2"

# ─────────────────────────────────────────────────────────────────
# Condition node export (regression for "unknown node type: 'condition'")
# ─────────────────────────────────────────────────────────────────
def _condition_workflow():
    """agent → condition(contains:hi / elseTarget) → branches.

    Mirrors the structure of the built-in `tpl-conditional-greeting`
    template so this doubles as a round-trip test for the seeded
    template.
    """
    return {
        "name": "cond-demo",
        "nodes": [
            {"id": "ngreet", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "Greeter",
                      "config": {"model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                                 "instructions": "say hi"}}},
            {"id": "nc", "type": "condition", "position": {"x": 0, "y": 0},
             "data": {"label": "Branch", "config": {
                 "condition": "contains:hi",
                 "elseTarget": "n_else",
             }}},
            {"id": "n_then", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "Friendly",
                      "config": {"model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                                 "instructions": "say welcome"}}},
            {"id": "n_else", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "Formal",
                      "config": {"model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                                 "instructions": "say hello"}}},
        ],
        "edges": [
            {"id": "e2", "source": "ngreet", "target": "nc"},
            {"id": "e3", "source": "nc", "target": "n_then"},  # first edge = then
            {"id": "e4", "source": "nc", "target": "n_else"},  # second edge = else
        ],
    }

def test_generator_renders_condition_node():
    """Condition node must produce Condition(...) + evaluator function,
    not raise GeneratorError('unknown node type: condition').

    The legacy `contains:hi` DSL auto-migrates to
    `evaluator.mode='function'` at save time. The generated Python
    emits a `_evaluator(step_input)` function whose body is the
    migrated Python expression — exposing the 5 new locals
    (previous_step_content, previous_step_outputs, input,
    additional_data, session_state) in scope.
    """
    code = render_python(_condition_workflow())
    # Module-level structure:
    assert "from agno.workflow.condition import Condition" in code
    assert "def nc_evaluator(step_input):" in code
    # The migrated expression references the value.
    assert "'hi'" in code
    assert "previous_step_content" in code
    # Pass-2 object emission:
    assert "nc_condition = Condition(" in code
    # Then-target and else-target are wired into steps=[...] / else_steps=[...]:
    assert "steps=[n_then_step]" in code
    assert "else_steps=[n_else_step]" in code
    # evaluator kwarg is the function above:
    assert "evaluator=nc_evaluator" in code

def test_generator_renders_condition_executes_into_workflow():
    """Round-trip: exec the generated module and verify the assembly
    includes the Condition object (and that its branches are NOT added
    as top-level steps — they're nested inside the Condition)."""
    import types

    code = render_python(_condition_workflow())
    ast.parse(code)  # syntax check first
    mod = types.ModuleType("agb_cond_exec")
    exec(compile(code, "<cond-test>", "exec"), mod.__dict__)

    from agno.workflow.condition import Condition

    assert hasattr(mod, "workflow"), "no `workflow` exported"
    assert hasattr(mod, "nc_condition"), "no Condition object"
    assert isinstance(mod.nc_condition, Condition)
    assert isinstance(mod.nc_condition.evaluator, type(lambda: None)), \
        "evaluator should be a callable"
    # Branches must NOT be top-level _steps — they're inside the Condition.
    step_names = [type(s).__name__ for s in mod.workflow.steps]
    assert step_names.count("Condition") == 1, \
        f"expected exactly 1 Condition at top level, got {step_names}"
    # n_then_step / n_else_step should be defined (referenced inside the
    # Condition) but NOT appended to the top-level steps list.
    assert hasattr(mod, "n_then_step")
    assert hasattr(mod, "n_else_step")

def test_generator_renders_condition_else_via_edge_only():
    """Condition with two outgoing edges and NO cfg.elseTarget should
    still emit `else_steps=[...]` and drop the second target from the
    top-level `_steps` list. The edge is the canonical, canvas-visible
    way to wire the else branch — the cfg fallback only exists for
    templates that pre-date the edge convention."""
    wf = _condition_workflow()
    # Strip cfg.elseTarget — only the second outgoing edge carries it.
    wf["nodes"][1]["data"]["config"].pop("elseTarget", None)
    code = render_python(wf)
    # Pass-2 object emission must include else_steps with the second edge's target:
    assert "nc_condition = Condition(" in code
    assert "else_steps=[n_else_step]" in code, (
        "condition with 2 outgoing edges should treat edge[1] as else_target; "
        "got code:\n" + code
    )
    # And the else branch must NOT also appear at top level (would otherwise
    # execute twice — once inside the Condition, once after it).
    assembly_start = code.index("workflow = Workflow(name=")
    assembly_block = code[assembly_start:]
    assert "_steps.append(n_else_step)" not in assembly_block
    assert "_steps.append(n_then_step)" not in assembly_block

def test_generator_renders_condition_with_equals_operator():
    """`equals:` operator should compile to `previous_step_content == "..."`.

    The legacy `equals:` operator migrated to
    `evaluator.mode='function'` — the generated
    expression uses the new `previous_step_content` local directly.
    """
    wf = _condition_workflow()
    wf["nodes"][1]["data"]["config"]["condition"] = "equals:hello"
    code = render_python(wf)
    assert "hello" in code
    assert "==" in code
    assert "previous_step_content" in code

def test_generator_renders_condition_always_and_never():
    """`always` / `never` migrate to `literal` mode —
    no `_evaluator` function is emitted; the evaluator kwarg is the
    literal value passed straight to `Condition(...)`."""
    for raw, kwarg in [("always", "evaluator=True"), ("never", "evaluator=False")]:
        wf = _condition_workflow()
        wf["nodes"][1]["data"]["config"]["condition"] = raw
        code = render_python(wf)
        # Literal mode skips the function — it passes the bool directly
        assert "def nc_evaluator" not in code
        assert kwarg in code

# ─────────────────────────────────────────────────────────────────
# Loop node export (regression for "unknown node type: 'loop'")
# ─────────────────────────────────────────────────────────────────
def _loop_workflow():
    """loop(bodyTarget=refine_agent).

    Mirrors the loop body pattern: the body agent is referenced via
    `cfg.bodyTarget` rather than an edge into the loop, so the DAG
    doesn't reach it through a normal walk — the generator must still
    emit its Step wrapper.

    (Originally pinned against `tpl-refine-until-done`; that template
    was retired in the template-gallery simplification since
    `tpl-iterative-story` covers the same pattern plus HITL.)
    """
    return {
        "name": "loop-demo",
        "nodes": [
            {"id": "nloop", "type": "loop", "position": {"x": 0, "y": 0},
             "data": {"label": "Refine", "config": {
                 "bodyTarget": "nrefine",
                 "maxIterations": 3,
                 "endCondition": "DONE",
                 "forwardIterationOutput": True,
             }}},
            {"id": "nrefine", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "RefineAgent",
                      "config": {"model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                                 "instructions": "refine until DONE"}}},
        ],
        "edges": [
            # Note: nrefine has no incoming/outgoing edge — only bodyTarget.
        ],
    }

def test_generator_renders_loop_node():
    """Loop node must produce Loop(...) with max_iterations / end_condition /
    forward_iteration_output, not raise GeneratorError."""
    code = render_python(_loop_workflow())
    assert "from agno.workflow.loop import Loop" in code
    assert "nloop_loop = Loop(" in code
    assert "steps=[nrefine_step]" in code
    assert "max_iterations=3" in code
    assert "end_condition=" in code
    # end_condition is emitted as a JSON-quoted string for agno's
    # substring-match semantics; raw `DONE` text should appear.
    assert "DONE" in code
    assert "forward_iteration_output=True" in code

def test_generator_renders_loop_executes_into_workflow():
    """Round-trip: exec the generated module and verify the loop assembles
    correctly with body reference but doesn't duplicate the body as a
    top-level step."""
    import types

    code = render_python(_loop_workflow())
    ast.parse(code)
    mod = types.ModuleType("agb_loop_exec")
    exec(compile(code, "<loop-test>", "exec"), mod.__dict__)

    from agno.workflow.loop import Loop

    assert hasattr(mod, "workflow"), "no `workflow` exported"
    assert hasattr(mod, "nloop_loop"), "no Loop object"
    assert isinstance(mod.nloop_loop, Loop)
    assert mod.nloop_loop.max_iterations == 3
    # end_condition is forwarded as-is (substring match on the body output).
    assert mod.nloop_loop.end_condition == "DONE"
    assert mod.nloop_loop.forward_iteration_output is True
    # The body agent's Step is referenced inside the Loop, not at top level.
    step_names = [type(s).__name__ for s in mod.workflow.steps]
    assert step_names.count("Loop") == 1
    assert step_names.count("Step") == 0, \
        f"body step should NOT be a top-level step, got {step_names}"

def test_generator_renders_loop_uses_outgoing_edge_when_no_bodyTarget():
    """Back-compat: if cfg.bodyTarget is missing but the loop has an
    outgoing edge, the generator falls back to the first outgoing node."""
    wf = {
        "name": "loop-fallback",
        "nodes": [
            {"id": "nloop", "type": "loop", "position": {"x": 0, "y": 0},
             "data": {"label": "Refine", "config": {"maxIterations": 2}}},
            {"id": "nrefine", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "RefineAgent",
                      "config": {"model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                                 "instructions": "refine"}}},
        ],
        "edges": [
            {"id": "e2", "source": "nloop", "target": "nrefine"},
        ],
    }
    code = render_python(wf)
    assert "nloop_loop = Loop(" in code
    assert "steps=[nrefine_step]" in code
    assert "max_iterations=2" in code

def test_generator_loop_omits_hitl_kwargs_by_default():
    """With no HITL flags set, the generated Loop(...)
    stays compact (no `requires_confirmation=False` noise). This keeps
    pre-HITL export snapshots byte-identical for users who don't opt in."""
    code = render_python(_loop_workflow())
    assert "nloop_loop = Loop(" in code
    assert "requires_confirmation" not in code
    assert "requires_iteration_review" not in code
    assert "confirmation_message" not in code
    assert "iteration_review_message" not in code

def test_generator_loop_emits_requires_confirmation_and_message():
    """`requiresConfirmation=True` + `confirmationMessage` flow through
    to the constructor kwargs verbatim."""
    wf = _loop_workflow()
    wf["nodes"][0]["data"]["config"]["requiresConfirmation"] = True
    wf["nodes"][0]["data"]["config"]["confirmationMessage"] = "Continue refining?"
    code = render_python(wf)
    assert "requires_confirmation=True," in code
    assert 'confirmation_message="Continue refining?",' in code
    assert "requires_iteration_review" not in code

def test_generator_loop_emits_requires_iteration_review_and_message():
    """Loop's distinguishing feature — ask before each iteration."""
    wf = _loop_workflow()
    wf["nodes"][0]["data"]["config"]["requiresIterationReview"] = True
    wf["nodes"][0]["data"]["config"]["iterationReviewMessage"] = "Next iteration?"
    code = render_python(wf)
    assert "requires_iteration_review=True," in code
    assert 'iteration_review_message="Next iteration?",' in code
    assert "requires_confirmation" not in code

def test_generator_loop_hitl_without_message_omits_message_kwarg():
    """Flag without message → only the flag kwarg is emitted; the
    `*_message` kwarg is skipped so agno uses its default prompt."""
    wf = _loop_workflow()
    wf["nodes"][0]["data"]["config"]["requiresConfirmation"] = True
    code = render_python(wf)
    assert "requires_confirmation=True," in code
    assert "confirmation_message" not in code

def test_generator_loop_hitl_assembles_into_agno_loop():
    """Emitted code is exec-able and the resulting Loop object has the
    HITL fields wired to the matching HumanReview fields."""
    import types
    from agno.workflow.loop import Loop

    wf = _loop_workflow()
    wf["nodes"][0]["data"]["config"]["requiresConfirmation"] = True
    wf["nodes"][0]["data"]["config"]["confirmationMessage"] = "Go?"
    wf["nodes"][0]["data"]["config"]["requiresIterationReview"] = True
    code = render_python(wf)
    mod = types.ModuleType("agb_loop_hitl_exec")
    exec(compile(code, "<loop-hitl-test>", "exec"), mod.__dict__)
    assert isinstance(mod.nloop_loop, Loop)
    assert mod.nloop_loop.requires_confirmation is True
    assert mod.nloop_loop.confirmation_message == "Go?"
    assert mod.nloop_loop.requires_iteration_review is True
    # Loophole: if user didn't set iterationReviewMessage, agno stores None.
    assert mod.nloop_loop.iteration_review_message is None

# ─────────────────────────────────────────────────────────────────
# Condition + Loop inside the all-types canary
# ─────────────────────────────────────────────────────────────────
def test_condition_and_loop_export_in_one_workflow():
    """Both new node types in a single workflow — confirms the
    generator's pass ordering handles them alongside agents/parallel."""
    wf = {
        "name": "cond-and-loop",
        "nodes": [
            {"id": "na", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A",
                      "config": {"model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                                 "instructions": "hi"}}},
            {"id": "nc", "type": "condition", "position": {"x": 0, "y": 0},
             "data": {"label": "C", "config": {"condition": "always"}}},
            {"id": "ncthen", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "Then",
                      "config": {"model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                                 "instructions": "then"}}},
            {"id": "nl", "type": "loop", "position": {"x": 0, "y": 0},
             "data": {"label": "L", "config": {"bodyTarget": "nlbody", "maxIterations": 1}}},
            {"id": "nlbody", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "Body",
                      "config": {"model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                                 "instructions": "body"}}},
        ],
        "edges": [
            {"id": "e2", "source": "na", "target": "nc"},
            {"id": "e3", "source": "nc", "target": "ncthen"},
            {"id": "e4", "source": "ncthen", "target": "nl"},
        ],
    }
    code = render_python(wf)
    # Both imports present:
    assert "from agno.workflow.condition import Condition" in code
    assert "from agno.workflow.loop import Loop" in code
    # Both objects present:
    assert "nc_condition = Condition(" in code
    assert "nl_loop = Loop(" in code
    # Then-target and body are referenced inside, not appended as top-level steps.
    assert "steps=[ncthen_step]" in code
    assert "steps=[nlbody_step]" in code
    # The whole module is syntactically valid + execs into a valid Workflow.
    ast.parse(code)
    import types
    mod = types.ModuleType("agb_combined")
    exec(compile(code, "<combined-test>", "exec"), mod.__dict__)
    from agno.workflow.condition import Condition
    from agno.workflow.loop import Loop
    assert isinstance(mod.nc_condition, Condition)
    assert isinstance(mod.nl_loop, Loop)
    # Top-level steps should be: the upstream `na` agent, plus the
    # Condition and Loop wrappers. The condition's then-target (`ncthen`)
    # and the loop's body (`nlbody`) are NESTED inside their parent —
    # they must NOT also appear at the top level.
    step_types = [type(s).__name__ for s in mod.workflow.steps]
    assert "Condition" in step_types and "Loop" in step_types, \
        f"expected Condition + Loop in top-level steps, got {step_types}"
    # The single top-level Step must be `na` (the upstream agent), NOT
    # `ncthen` (condition's then-target) or `nlbody` (loop's body).
    assert step_types.count("Step") == 1
    top_step = mod.workflow.steps[step_types.index("Step")]
    assert getattr(top_step, "name", None) == "A", \
        f"upstream agent should be `A`, got {top_step.name!r}"

# ─────────────────────────────────────────────────────────────────
# Examples / smoke tests — run scripts/dump_examples.py, then verify
# every emitted sample is parseable AND can be exec()'d into a module
# with a valid Workflow + steps. This is the "did we break the export?"
# canary. Skipped if the optional `requests` package isn't installed
# (the http_call / full_stack samples depend on it).
# ─────────────────────────────────────────────────────────────────
@pytest.mark.skipif(
    not _importlib_util.find_spec("requests"),
    reason="`requests` not installed; HTTP-dependent samples can't exec",
)
def test_dumped_examples_parse_and_exec(tmp_path):
    """Re-generate the /examples/ files into tmp_path, then parse + exec each.

    We don't touch the real /examples/ directory — we just want to confirm
    the generator still produces runnable code for every canonical shape.
    """
    # 1. Run dump_examples.py with its output dir redirected to tmp_path.
    spec = _importlib_util.spec_from_file_location("dump_examples", _DUMP_SCRIPT)
    assert spec and spec.loader, "dump_examples.py missing or unloadable"
    dump_mod = _importlib_util.module_from_spec(spec)
    spec.loader.exec_module(dump_mod)
    # monkey-patch the output dir to tmp_path, then re-run main()
    dump_mod.EXAMPLES_DIR = tmp_path
    dump_mod.main()

    samples = sorted(tmp_path.glob("*.py"))
    assert len(samples) == len(dump_mod.SAMPLES), \
        f"expected {len(dump_mod.SAMPLES)} samples, got {len(samples)}"

    for path in samples:
        code = path.read_text(encoding="utf-8")
        # (a) parses
        _ast.parse(code)
        # (b) exec()s cleanly into a module with a Workflow + steps list
        mod = _types.ModuleType(path.stem)
        exec(compile(code, str(path), "exec"), mod.__dict__)  # noqa: S102
        assert hasattr(mod, "workflow"), f"{path.name}: missing `workflow`"
        assert hasattr(mod, "_steps"), f"{path.name}: missing `_steps`"
        assert isinstance(mod.workflow.steps, list)
        # Every step is a real agno Step / Parallel / Router object.
        from agno.workflow.step import Step
        from agno.workflow.parallel import Parallel
        from agno.workflow.router import Router
        for s in mod.workflow.steps:
            assert isinstance(s, (Step, Parallel, Router)), \
                f"{path.name}: unexpected step type {type(s).__name__}"

def test_dumped_examples_match_committed_files(tmp_path):
    """If the committed samples in /examples/ drift out of sync with what the
    generator currently emits, this fails. Run `scripts/dump_examples.py` to
    refresh them. Skips samples that depend on `requests` if it's not present.
    """
    if not _EXAMPLES_DIR.exists():
        pytest.skip("/examples/ not present in this checkout")
    needs_requests = {"http_call.py", "full_stack.py"}
    has_requests = _importlib_util.find_spec("requests") is not None

    for committed in sorted(_EXAMPLES_DIR.glob("*.py")):
        if committed.name in needs_requests and not has_requests:
            continue
        code = committed.read_text(encoding="utf-8")
        # (a) parses
        try:
            _ast.parse(code)
        except SyntaxError as e:
            pytest.fail(f"{committed.name} no longer parses: {e}")
        # (b) exec()s
        mod = _types.ModuleType(committed.stem)
        try:
            exec(compile(code, str(committed), "exec"), mod.__dict__)
        except Exception as e:
            pytest.fail(f"{committed.name} no longer exec()s: {type(e).__name__}: {e}")
        assert hasattr(mod, "workflow"), f"{committed.name}: no `workflow`"

# ─────────────────────────────────────────────────────────────────
# tool_attachment edges drive `tools=[...]` wiring
# ─────────────────────────────────────────────────────────────────
def test_tool_attachment_edge_wires_function_into_agent():
    """Edge with `tool_attachment` kind from a function-source
    `tool` node to an agent must produce the same `agent.tools = [...]`
    line that legacy `cfg.toolsRef` produced (pass 3 source:
    ir.tool_attachments)."""
    wf = {
        "name": "tools-edge",
        "nodes": [
            {"id": "nt", "type": "tool", "position": {"x": 0, "y": 0},
             "data": {"label": "tools", "config": {"source": "function", "functions": [
                 {"name": "add", "description": "add", "body": "def add(a, b):\n    return a + b\n"},
             ]}}},
            {"id": "na", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A", "config": {
                 "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                 "instructions": "use add",
             }}},
        ],
        "edges": [
            {"id": "e1", "source": "nt", "target": "na", "kind": "tool_attachment"},
        ],
    }
    code = render_python(wf)
    # The wiring line is emitted (pass 3 reads ir.tool_attachments).
    assert "na_agent.tools" in code
    assert "Function.from_callable(add" in code or "add" in code

def test_tool_attachment_edge_keeps_tools_out_of_top_level_steps():
    """A function-source `tool` node with ONLY a tool_attachment
    edge must NOT appear as a top-level step in `Workflow(steps=[...])` — it's a
    definition-only node, never an executable step."""
    wf = {
        "name": "tools-edge-no-top",
        "nodes": [
            {"id": "nt", "type": "tool", "position": {"x": 0, "y": 0},
             "data": {"label": "tools", "config": {"source": "function", "functions": [
                 {"name": "f", "description": "f", "body": "def f():\n    return 1\n"},
             ]}}},
            {"id": "na", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A", "config": {
                 "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                 "instructions": "use f",
             }}},
        ],
        "edges": [
            {"id": "e1", "source": "nt", "target": "na", "kind": "tool_attachment"},
        ],
    }
    code = render_python(wf)
    # The `tools` node must NOT show up as a Step in Workflow(steps=[...]).
    assert "nt_step" not in code
    # The agent is the only top-level step.
    assert "na_step" in code

def test_tool_attachment_http_edge_wires_into_agent():
    """An `http` tool-source node wired to an agent via tool_attachment
    edge is wrapped as a Python function (HTTP wrapper) and registered
    on the agent via Function.from_callable(...).

    # The legacy `http` type is now `tool` with `source='http'`.
    # The `headers` field is a dict, not a list — the original
    # test fixture had a stale `[]` list which the pre-merge
    # emitter tolerated as `extra='ignore'`. The current
    # validator rejects it with a `dict_type` error."""
    wf = {
        "name": "http-edge",
        "nodes": [
            {"id": "nh", "type": "tool", "position": {"x": 0, "y": 0},
             "data": {"label": "Get", "config": {"source": "http",
                 "toolName": "fetch_user",
                 "toolDescription": "Get user",
                 "baseUrl": "https://api.example.com",
                 "method": "GET",
                 "path": "/users/{id}",
                 "params": [],
                 "headers": {},
             }}},
            {"id": "na", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A", "config": {
                 "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                 "instructions": "use fetch_user",
             }}},
        ],
        "edges": [
            {"id": "e1", "source": "nh", "target": "na", "kind": "tool_attachment"},
        ],
    }
    code = render_python(wf)
    assert "na_agent.tools" in code
    assert "fetch_user" in code

def test_tool_attachment_mcp_edge_wires_into_agent():
    """An `mcp` tool-source node wired to an agent via tool_attachment
    edge is referenced in the agent's `tools=[...]` list."""
    wf = {
        "name": "mcp-edge",
        "nodes": [
            {"id": "nm", "type": "tool", "position": {"x": 0, "y": 0},
             "data": {"label": "FS", "config": {"source": "mcp", 
                 "serverId": "mcp-fs",
                 "toolNamePrefix": "fs_",
             }}},
            {"id": "na", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A", "config": {
                 "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                 "instructions": "use fs",
             }}},
        ],
        "edges": [
            {"id": "e1", "source": "nm", "target": "na", "kind": "tool_attachment"},
        ],
    }
    code = render_python(wf)
    # The MCP node is constructed as a Python object — referenced from
    # the agent's tools list.
    assert "nm_mcp" in code
    assert "na_agent.tools" in code

def test_cfg_toolsref_still_works_as_fallback():
    """Pre-migration workflows using `cfg.toolsRef` (no typed edge) keep
    working: the IR promotes cfg entries into tool_attachments so
    pass 3 emits the wiring line."""
    wf = {
        "name": "legacy-tools",
        "nodes": [
            {"id": "nt", "type": "tool", "position": {"x": 0, "y": 0},
             "data": {"label": "tools", "config": {"source": "function", "functions": [
                 {"name": "add", "description": "add", "body": "def add(a, b):\n    return a + b\n"},
             ]}}},
            {"id": "na", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A", "config": {
                 "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                 "instructions": "use add",
                 "toolsRef": ["nt"],
             }}},
        ],
        "edges": [],
    }
    code = render_python(wf)
    assert "na_agent.tools" in code

def test_dataflow_edge_from_tool_source_is_rejected_by_validator():
    """A `kind=None` edge from a tool-source node to an agent is
    rejected by the connection validator at save time — the generator
    never sees it. Pin that behaviour here so the invariant is
    documented in one place."""
    from app.core.compile import CompileError as GeneratorError
    wf = {
        "name": "tool-as-dataflow",
        "nodes": [
            {"id": "nt", "type": "tool", "position": {"x": 0, "y": 0},
             "data": {"label": "tools", "config": {"source": "function", "functions": [
                 {"name": "f", "description": "f", "body": "def f():\n    return 1\n"},
             ]}}},
            {"id": "na", "type": "agent", "position": {"x": 0, "y": 0},
             "data": {"label": "A", "config": {
                 "model": {"provider": "openai", "modelId": "gpt-4o", "apiKey": "k"},
                 "instructions": "use f",
             }}},
        ],
        # kind=None → dataflow; validator rejects this.
        "edges": [
            {"id": "e1", "source": "nt", "target": "na"},
        ],
    }
    with pytest.raises(GeneratorError):
        render_python(wf)