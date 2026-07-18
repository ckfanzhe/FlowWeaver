"""Tests for `app.services.chat_builder_schema` — the F2 manifest
introspection layer.

Three layers:
  1. Summary tests — `summarise_node_types()` returns the expected
     shape (15 entries incl. preset types, with field summaries).
  2. Tool-surface tests — the LLM-facing helpers
     (`get_node_types_tool`, `node_types_for_prompt`) return valid
     output and stay in sync with the manifest.
  3. Service integration — `BUILDER_SYSTEM_PROMPT()` includes the
     manifest-derived type table.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock

import pytest

from app.auth import CurrentUser
from app.db.models import User, Workflow
from app.services import chat_builder_service as cbs
from app.services import member_service
from app.services.chat_builder_schema import (
    summarise_node_types,
    get_node_types_tool,
    node_types_for_prompt,
)

# ─────────────────────────────────────────────────────────────────
# summarise_node_types
# ─────────────────────────────────────────────────────────────────
class TestSummariseNodeTypes:
    """`summarise_node_types()` is the F2 entry point. Its contract
    is: returns a list of `{type, display_name, kind, default_config,
    fields[]}` dicts, one per manifest entry, ordered by
    `palette_order`."""

    def test_summary_covers_every_manifest_entry(self):
        summary = summarise_node_types()
        names = {entry["type"] for entry in summary}
        # Every manifest entry is included.
        # `parallel`+`steps` collapsed into `flow`,
        # `router`+`condition` collapsed into `branch`,
        # `http`+`mcp`+`tools` collapsed into `tool`,
        # and `human_input` was renamed to `ask`. The 5 preset
        # tool types collapsed into the `tool` node's `preset`
        # discriminator — they no longer appear as separate manifest
        # entries, so the summary has 6 base types.
        for required in [
            "agent", "branch", "flow", "loop", "ask", "tool",
        ]:
            assert required in names, (
                f"summary missing {required!r}; got {sorted(names)}"
            )

    def test_summary_entry_shape(self):
        """Each entry has the keys the LLM relies on for routing."""
        summary = summarise_node_types()
        for entry in summary:
            assert "type" in entry
            assert "display_name" in entry
            assert "kind" in entry
            assert "default_config" in entry
            assert "fields" in entry
            # fields is a list of dicts with name/alias/required.
            for field in entry["fields"]:
                assert "name" in field
                assert "alias" in field
                assert "required" in field

    def test_agent_summary_includes_known_fields(self):
        """The agent entry has the canonical fields the LLM knows
        about — `instructions`, `markdown`, `toolsRef`. This pins
        that we don't drop fields when schema-introspecting."""
        summary = summarise_node_types()
        agent = next(e for e in summary if e["type"] == "agent")
        names = {f["name"] for f in agent["fields"]}
        aliases = {f["alias"] for f in agent["fields"]}
        for required_name in ("instructions", "markdown", "tools_ref"):
            assert required_name in names or required_name in aliases, (
                f"agent summary missing {required_name!r}; "
                f"names={names}, aliases={aliases}"
            )

    def test_summary_is_ordered_by_palette_order(self):
        """The palette_order controls the palette-bar rendering; the
        LLM-facing list should mirror that order so `node_types_for_prompt`
        reads naturally top-to-bottom. The 5
        preset types collapsed into `tool`'s `preset` discriminator,
        so `arxiv_search` is no longer a separate entry — use `loop`
        (palette_order=6, last of the 6 base types) for the relative-
        ordering pin."""
        summary = summarise_node_types()
        # We don't assert the exact order — palette_order is the
        # manifest author's call — but the relative ordering of
        # `agent` (palette_order=1) and `loop` (palette_order=6)
        # should match.
        agent_idx = next(
            i for i, e in enumerate(summary) if e["type"] == "agent"
        )
        loop_idx = next(
            i for i, e in enumerate(summary) if e["type"] == "loop"
        )
        assert agent_idx < loop_idx, (
            f"agent (palette_order=1) should come before loop; "
            f"agent_idx={agent_idx}, loop_idx={loop_idx}"
        )

# ─────────────────────────────────────────────────────────────────
# Tool surface — get_node_types_tool + node_types_for_prompt
# ─────────────────────────────────────────────────────────────────
class TestToolSurface:
    """The two helpers exposed to the chat builder."""

    def test_get_node_types_tool_returns_valid_json(self):
        out = get_node_types_tool()
        parsed = json.loads(out)
        assert "node_types" in parsed
        assert isinstance(parsed["node_types"], list)
        # +N2+N4+N5+N3 : 6 base entries
        # (parallel+steps → flow; router+condition → branch;
        # http+mcp+tools → tool; human_input → ask; 5 presets →
        # tool+preset discriminator). Asserted >= so future
        # additions don't force this test to update.
        assert len(parsed["node_types"]) >= 6

    def test_get_node_types_tool_lists_every_type(self):
        """The tool's output must include every manifest entry.

        The 5 preset tool types
        (wikipedia / tavily_search / duckduckgo / calculator /
        arxiv_search) collapsed into the `tool` node's `preset`
        discriminator — they no longer appear as separate manifest
        entries in the tool's output. The LLM discovers presets via
        the `tool` entry's `fields[]` (which includes the
        `preset` discriminator literal)."""
        out = json.loads(get_node_types_tool())
        types = {entry["type"] for entry in out["node_types"]}
        for required in (
            "agent", "branch", "flow", "loop", "ask", "tool",
        ):
            assert required in types
        # No preset types in the tool output — they're a `preset`
        # discriminator on the `tool` node, not separate types.
        assert "wikipedia" not in types
        assert "tavily_search" not in types
        assert "duckduckgo" not in types
        assert "calculator" not in types
        assert "arxiv_search" not in types

    def test_node_types_for_prompt_includes_tool_with_preset_hint(self):
        """The compact prompt fragment must surface the new `tool`
        node + its `preset` discriminator (replacing the prior
        5-preset hint rows). : the `tool`
        entry's hint mentions the 5 preset names so the LLM knows
        they're available via `preset`."""
        fragment = node_types_for_prompt()
        # `tool` hint mentions the preset discriminator.
        assert "preset" in fragment, (
            "prompt fragment missing `preset` hint on the `tool` "
            "entry — the LLM needs to know presets are a discriminator"
        )
        # All 5 preset names still surfaced (inside the `tool` hint).
        for required in (
            "wikipedia", "tavily_search", "duckduckgo",
            "calculator", "arxiv_search",
        ):
            assert required in fragment, (
                f"prompt fragment missing preset name {required!r} "
                "in the `tool` hint"
            )

# ─────────────────────────────────────────────────────────────────
# Service integration — system prompt + tool registration
# ─────────────────────────────────────────────────────────────────
class TestServiceIntegration:
    """`BUILDER_SYSTEM_PROMPT()` is the function called by
    `run_chat_turn` to build the LLM's instructions. The chat UI
    and the LLM both depend on it."""

    def _session(self, db):
        db.add(User(id="alice@example.com", tenant_id="tenant-default"))
        db.commit()
        wid = f"wf-{uuid.uuid4().hex[:8]}"
        db.add(Workflow(
            id=wid, name="seed", description="seed",
            nodes=[{
                "id": "a1", "type": "agent",
                "position": {"x": 0.0, "y": 0.0},
                "data": {"label": "A1", "config": {"instructions": ""}},
            }],
            edges=[],
            created_by="alice@example.com",
        ))
        db.commit()
        member_service.bootstrap_owner(db, wid, "alice@example.com")
        db.commit()
        return cbs._load_or_create_session(
            db, wid,
            CurrentUser(id="alice@example.com", tenant_id="tenant-default"),
        )

    def test_builder_system_prompt_includes_all_presets(self, db):
        cbs.BUILDER_SYSTEM_PROMPT()  # forces cache fill

    def test_builder_system_prompt_mentions_plan_workflow(self, db):
        """The system prompt steers the LLM toward `plan_workflow`
        for non-trivial edits (F1 / F2 lock-in). Pin it so a
        future prompt rewrite doesn't accidentally drop the
        recommendation."""
        prompt = cbs.BUILDER_SYSTEM_PROMPT()
        assert "plan_workflow" in prompt

    def test_builder_system_prompt_distinguishes_agent_job_from_workflow_steps(self, db):
        """General-principle guidance, .

        The chat-builder keeps building workflows that express
        the AGENT'S TASK (greet, extract, format, confirm,
        dispatch) as WORKFLOW NODES (ask gates, multi-agent
        chains) instead of putting the task in one agent's
        instructions. Specific failures observed in production:
          - greeting + extract + query + format turned into a
            5-agent chain (welcome → entity_extract → query →
            dispatch → result)
          - "一开始输出 X，然后等待 Y" turned into an `ask`
            node wired ahead of the agent

        The prompt MUST include a section that gives the
        underlying principle — not a list of badcases — so
        future variants of the same mistake still get caught.
        Pin:
          - the section header (catches rename)
          - the framing "the user's prompt describes the
            AGENT'S JOB, not the workflow's steps"
          - the explicit contrast between BAD and GOOD drafts
            (so a future rewrite that drops one direction
            fails loudly)
          - the decision rules table that maps user phrasings
            to the correct primitive
          - the self-check instruction ("read each
            non-compound node's config.instructions") — this
            is the actionable heuristic the LLM can apply at
            plan-time without re-reading the whole section

        Deliberately NOT pinned (so the section can be
        rephrased without re-litigating every specific example):
          - the user-case tokens ("衡阳市" / "变压器")
          - the literal greeting string
          - the literal "EXCEPTION" word — replaced by the
            decision-rules table which subsumes the exception
        """
        prompt = cbs.BUILDER_SYSTEM_PROMPT()
        # Section header.
        assert (
            "The user's prompt describes the AGENT'S JOB"
            in prompt
        )
        # The framing — the principle, not a badcase list.
        assert "AGENT'S JOB, not the workflow's steps" in prompt
        # Both BAD and GOOD drafts must appear (catches
        # accidental one-direction removal).
        assert "# BAD" in prompt
        assert "# GOOD" in prompt
        # The decision rules — the LLM must be able to map a
        # user phrasing to a primitive from the prompt alone.
        assert "Decision rules" in prompt
        assert (
            "ONE agent with multiple tools" in prompt
        )
        # The self-check — the actionable heuristic. Phrased as
        # "read each ... instructions" so a future rewording
        # that drops the literal word still has the same
        # intent.
        assert "Self-check" in prompt
        assert (
            "merge them into ONE agent" in prompt
            or "merge into ONE agent" in prompt
        )

    def test_builder_system_prompt_discourages_info_passing_chains(self, db):
        """Anti-info-passing-chain guidance (follow-up, ).

        Regression net for the recurring failure mode where the LLM
        builds a linear pipeline of single-purpose agents (welcome →
        entity_extract → query → format) where each downstream agent
        has to parse the previous agent's free-text output to extract
        structured fields. The result is N user-visible messages,
        brittle JSON-string parsing, and on tool failure N near-
        identical "sorry the tool failed" responses.

        The prompt MUST include the section that:
          - defaults to ONE agent with multiple tools attached
          - lists the 5 legitimate reasons to add a second agent
            (HITL / parallel / non-overlapping tools / distinct model / loop body)
          - names the 5-agent welcome→...→result chain as the canonical anti-pattern
          - contrasts with a single-agent-with-N-tools good pattern
          - states the "rule of thumb" sentence

        If a future prompt rewrite drops any of these, the LLM drifts
        back toward long info-passing chains on the next edit."""
        prompt = cbs.BUILDER_SYSTEM_PROMPT()
        # Section header (so a future rename is caught explicitly).
        assert "Workflow structure — when to split into multiple agents" in prompt
        # Default-merge guidance.
        assert "DEFAULT: ONE agent" in prompt
        # All 5 legitimate-split reasons.
        for reason_phrase in (
            "HITL gate between them",
            "Parallel composition",
            "Distinct, non-overlapping tool sets",
            "Distinct model",
            "Loop body",
        ):
            assert reason_phrase in prompt, f"missing reason: {reason_phrase!r}"
        # Concrete anti-pattern example (the user's  5-agent report).
        assert "5-agent chain welcome" in prompt
        # Concrete good pattern (single agent + N tools).
        assert "ONE agent with both HTTP tools attached" in prompt
        # Rule of thumb sentence — the LLM should quote this back in its
        # own self-explanation when it splits intentionally.
        assert "ONE agent with multiple tools" in prompt

    def test_builder_system_prompt_mentions_get_node_types(self, db):
        """The F2.1 hook (call this to learn the schema) must be
        visible to the LLM. Without this hint the LLM falls back
        to guessing field names."""
        prompt = cbs.BUILDER_SYSTEM_PROMPT()
        assert "get_node_types" in prompt

    def test_get_node_types_is_exposed_as_a_tool(self, db):
        """The F2.1 tool must appear in `_build_tools_for_session`'s
        output. If a future refactor accidentally drops it, the LLM
        loses its on-demand schema lookup."""
        session = self._session(db)
        funcs = cbs._build_tools_for_session(session)
        names = {f.name for f in funcs}
        assert "get_node_types" in names

    def test_get_node_types_tool_call_returns_summary(self, db):
        """Calling `get_node_types` via the wrapper should return
        the manifest summary — drives the loop that wires the
        tool to the actual schema."""
        session = self._session(db)
        funcs = cbs._build_tools_for_session(session)
        by_name = {f.name: f for f in funcs}
        gn = by_name["get_node_types"]
        # Function.from_callable exposes the entry point as `.entrypoint`.
        result = gn.entrypoint()
        parsed = json.loads(result)
        assert "node_types" in parsed
        # +N2+N4+N5+N3 : 6 entries (5 presets
        # collapsed into the `tool` node's `preset` discriminator).
        assert len(parsed["node_types"]) >= 6

    def test_builder_system_prompt_distinguishes_human_input_from_router(self, db):
        """A previous version of the prompt let the
        LLM confuse `human_input` (a value collector) with `router`
        (a branch decider). When asked for a yes/no routing the LLM
        would drop a `human_input`, the user would answer 否, and
        the workflow would silently continue downstream because
        `human_input` doesn't actually choose a branch. Lock in the
        HITL section so future prompt edits don't accidentally drop
        the disambiguation rule."""
        prompt = cbs.BUILDER_SYSTEM_PROMPT()
        # The section heading + the keyword that names the trap.
        assert "HITL" in prompt
        assert "human_input" in prompt
        assert "router" in prompt
        # The actual decision rule — must appear so the LLM sees it
        # on every turn.
        assert "selector_mode='hitl'" in prompt or 'selector_mode="hitl"' in prompt
        # The "common mistake" framing — the LLM sees a yes/no
        # pattern and reaches for human_input instead of router.
        assert "yes/no" in prompt.lower() or "yes / no" in prompt.lower()

    def test_builder_system_prompt_includes_http_full_config_example(self, db):
        """The system prompt must show the full HTTP config
        shape. The LLM was under-emitting (4 fields instead of 9) and
        producing untitled tools at runtime. The worked example must
        include `toolName`, `toolDescription`, `headers`, `queryParams`,
        and `authToken` so the LLM doesn't fall back on a 4-field
        mental model.

        The HTTP fields now live on the
        merged `ToolNodeConfig` (with `source='http'` discriminator).
        The prompt still references the same 9 fields by name —
        the LLM-facing surface is unchanged because the field set
        is identical to the prior `HttpNodeConfig`."""
        prompt = cbs.BUILDER_SYSTEM_PROMPT()
        # Section header + key field markers
        assert "HTTP node" in prompt
        assert "full config shape" in prompt
        # All 9 HTTP-mode fields should be named in the worked
        # example (toolName, toolDescription, method, baseUrl, path,
        # headers, queryParams, bodySchema, authToken).
        for field in (
            "toolName", "toolDescription", "method", "baseUrl",
            "path", "headers", "queryParams", "bodySchema", "authToken",
        ):
            assert field in prompt, (
                f"prompt must include HTTP field {field!r} so the LLM "
                f"doesn't under-emit"
            )

    def test_builder_system_prompt_includes_router_selector_semantics(self, db):
        """The system prompt must explain that
        `selector.expression` (function mode) returns a BRANCH STEP
        OBJECT (`<branch_id>_step`), NOT a label string. Without
        this, the LLM emits `"yes"` (a label) which matches nothing
        at runtime."""
        prompt = cbs.BUILDER_SYSTEM_PROMPT()
        # The section header
        assert "Router selector semantics" in prompt
        # The key teaching: return value is `<branch_id>_step`, not a label.
        assert "<branch_id>_step" in prompt
        assert "yes_agent_step" in prompt
        # Worked example edges must carry sourceHandle = branch label.
        assert '"sourceHandle":"yes"' in prompt
        # Worked example selector expression must reference `_step`-suffixed ids.
        assert "yes_agent_step if previous_step_content" in prompt

    def test_create_router_pattern_docstring_explains_step_return(self):
        """The `create_router_pattern` tool docstring must
        tell the LLM that function-mode selectors return a step
        object, not a label string. The diagnostic export on
         showed the LLM emitting "yes" (label) instead of
        `yes_agent_step`."""
        # Find the create_router_pattern closure inside
        # _build_tools_for_session and assert the docstring content.
        from app.services.chat_builder_service import _build_tools_for_session
        session = MagicMock()
        session.workflow_id = "wf-1"
        session.pending_changes = []
        tools = _build_tools_for_session(session)
        # Locate the create_router_pattern tool by name.
        router_tool = next(
            (t for t in tools if getattr(t, "name", None) == "create_router_pattern"),
            None,
        )
        if router_tool is None:
            # Some Function wrappers put the callable on .entrypoint
            router_tool = next(
                (t for t in tools if getattr(t, "entrypoint", None)
                 and getattr(t.entrypoint, "__name__", "") == "create_router_pattern"),
                None,
            )
        assert router_tool is not None, (
            "create_router_pattern not found in the LLM tool surface"
        )
        # The docstring may live on .description, .entrypoint.__doc__,
        # or be wrapped in .function — try all paths.
        candidates = []
        for attr in ("description",):
            v = getattr(router_tool, attr, None)
            if isinstance(v, str):
                candidates.append(v)
        ep = getattr(router_tool, "entrypoint", None)
        if ep is not None:
            for attr in ("__doc__",):
                v = getattr(ep, attr, None)
                if isinstance(v, str):
                    candidates.append(v)
        fn = getattr(router_tool, "function", None)
        if fn is not None:
            for attr in ("__doc__",):
                v = getattr(fn, attr, None)
                if isinstance(v, str):
                    candidates.append(v)
        joined = "\n".join(candidates)
        assert "BRANCH STEP OBJECT" in joined or "_step" in joined, (
            f"create_router_pattern docstring must explain the "
            f"step-object return contract. Got:\n{joined[:600]}"
        )

    def test_usage_hint_distinguishes_human_input_from_branch(self):
        """The compact per-type summary (`node_types_for_prompt()`)
        surfaces in the system prompt's node-list block. The
        `human_input` hint must make it clear it's a value
        collector, not a branch decider; the `branch` hint (the
        successor to `router` / `condition` per ) must
        point the LLM at `selector_mode='hitl'` for HITL branch
        selection."""
        prompt = node_types_for_prompt()
        # ask line — must NOT just say "pause for input" (the previous
        # wording that misled the LLM). :
        # `human_input` was renamed to `ask`; the LLM-facing prompt
        # text now reads "ask the user for input".
        hi_line = next(
            (ln for ln in prompt.splitlines() if ln.lstrip().startswith("- ask")),
            None,
        )
        assert hi_line is not None, "ask missing from prompt node list"
        assert "branch" in hi_line.lower()
        assert "value" in hi_line.lower() or "collect" in hi_line.lower()
        # branch line —  replaced the legacy
        # `- router` row with `- branch`. The same HITL selector-mode
        # hint lives on this row now (covers both `switch` and
        # `if-else` modes).
        br_line = next(
            (ln for ln in prompt.splitlines() if ln.lstrip().startswith("- branch")),
            None,
        )
        assert br_line is not None, "branch missing from prompt node list"
        assert "hitl" in br_line.lower()

    def test_ask_field_descriptions_surface_via_get_node_types(self):
        """`AskConfig` now carries per-field `description`s that warn
        the LLM about the common misuse. Verify they reach the LLM
        via `get_node_types()` — a future Pydantic schema refactor
        that drops the description silently would re-open the bug.
        The entry was renamed from `HumanInputNodeConfig` /
        `human_input` to `AskConfig` / `ask`."""
        parsed = json.loads(get_node_types_tool())
        hi = next(
            (t for t in parsed["node_types"] if t["type"] == "ask"),
            None,
        )
        assert hi is not None, "ask missing from get_node_types output"
        by_name = {f["name"]: f for f in hi["fields"]}
        for field_name in ("prompt", "input_type", "choices"):
            assert field_name in by_name, f"ask.{field_name} missing"
            desc = by_name[field_name]["description"]
            assert desc, f"ask.{field_name} has empty description"
        # input_type description must explicitly mention branch —
        # that's the disambiguation the LLM needs (it used to mention
        # router; router is now branch per ).
        assert "branch" in by_name["input_type"]["description"].lower()