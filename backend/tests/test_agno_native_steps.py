"""Agent native steps — verify agent nodes run via the agno-native path.

Before the agent-strategy fold, every agent step was constructed as
`Step(executor=_legacy_handler_wrapper)` where the wrapper:
  - resolved the model's default preset / inline config,
  - called `_agent_handler` which built its own `Agent` inside
    `llm_runner.call_model_sync`,
  - returned a `HandlerResult` with `TextEvent`,
  - encoded errors via the `[error] ` sentinel for EventAdapter.

After stage 1, the runtime constructs `Step(agent=Agent(model=...,
instructions=..., markdown=...))` directly. `Agent.run()` produces
`RunContentEvent` (or equivalent), EventAdapter translates it to
`TextEvent`. The legacy `_agent_handler` is gone; `_REGISTRY` no
longer has an `"agent"` key.

These tests pin the new contract:
  - A linear agent workflow runs to completion with the expected
    text payload (a TextEvent carrying the LLM's reply, then a
    CompletedEvent). We use real `.env.llm` credentials via the
    `real_llm_preset` fixture so the agent actually calls
    the LLM and EventAdapter translates the resulting
    `RunContentEvent` to `TextEvent`. (Earlier drafts stubbed the
    model class with a canned `_EchoResponse`; that approach was
    dropped because it didn't exercise the SSE event ordering
    faithfully.)
  - An Agent node with no resolvable model returns a single
    ErrorEvent (matching the legacy contract — the front-end error
    trace panel depends on exactly one ErrorEvent per failing node).
  - The exported Python still works — the generator emits
    `Step(agent=Agent(...))` already; the runtime fold only changed
    the runtime path, so the export shape is unchanged. Pin it
    here so a future refactor can't silently re-divergence the
    two.

Why a separate test file (rather than extending test_runtime.py):
  - The migration was substantial enough that a dedicated file
    makes the intent explicit in the test suite name.
  - Future native-path coverage (router/parallel/condition/loop
    → native) will append to this file with its own classes so
    the "AGNO-native path" coverage stays together.
"""
from __future__ import annotations

import pytest

from app.core.events import (
    CompletedEvent,
    ErrorEvent,
    TextEvent,
)

# ─────────────────────────────────────────────────────────────────
# 1. Happy path — single agent runs via Step(agent=Agent(...))
# ─────────────────────────────────────────────────────────────────
class TestAgentNativeRun:
    def test_linear_echo_emits_text_then_completed(self, real_llm_preset):
        """The canonical happy path. After the agent-strategy fold, agent nodes
        run through `Step(agent=Agent(...))` and `real_llm_preset`
        seeds a Claude preset loaded with real `.env.llm` credentials
        (no more stubbed `_EchoResponse` — the agent actually calls
        the LLM, and EventAdapter translates the resulting
        `RunContentEvent` to `TextEvent`).

        We assert on event *shape* (TextEvent then CompletedEvent) and
        that the LLM produced a non-empty response. The exact text is
        not pinned — that would make the test brittle to LLM changes
        and would couple it to network availability. The legacy
        `[label] echo:` prefix assertion is dropped because we no
        longer fake the model output."""
        from app.core.compile import run_leg

        nodes = [{
            "id": "ag",
            "type": "agent",
            "position": {"x": 0, "y": 0},
            "data": {"label": "Bot", "config": {}},
        }]
        edges = []
        sid, events, _wf = run_leg(workflow_id="wf-native-1", name="wf-native-1", db_nodes=nodes, db_edges=edges, input="hello")

        # User-visible events in order: a stream of TextEvents (one per
        # LLM token / chunk), NodeStart/NodeEnd around them, then the
        # workflow-level CompletedEvent. The exact count of streamed
        # TextEvents depends on the LLM's chunking — we only pin the
        # structure (≥1 TextEvent, then exactly one CompletedEvent
        # at the end).
        completed_events = [e for e in events if isinstance(e, CompletedEvent)]
        text_events = [e for e in events if isinstance(e, TextEvent)]
        assert len(text_events) >= 1, (
            f"expected at least one TextEvent from the LLM, got {len(text_events)}; "
            f"full events: {[type(e).__name__ for e in events]}"
        )
        assert len(completed_events) == 1, (
            f"expected exactly one CompletedEvent, got {len(completed_events)}"
        )
        # The CompletedEvent must be the LAST event on the stream —
        # anything else would corrupt the SSE event-ordering contract
        # the frontend's chat panel depends on.
        assert events[-1] is completed_events[0], (
            f"CompletedEvent must be the last event; got {events[-1]!r}"
        )

        # The LAST TextEvent carries the full accumulated reply (the
        # earlier TextEvents are chunk-level streaming deltas). The
        # CompletedEvent's output may add surrounding whitespace
        # (the agent has `markdown=True` by default and EventAdapter
        # appends newlines around block elements) — so we check that
        # the TextEvent's content is *contained* in the CompletedEvent
        # output rather than asserting strict equality. That keeps
        # the test resilient to formatting noise while still pinning
        # the EventAdapter stitching contract.
        last_text = text_events[-1]
        assert last_text.content and len(last_text.content) > 0, (
            "expected the real LLM to produce a non-empty reply; "
            "if this fails, check that `.env.llm` carries valid credentials."
        )
        assert last_text.content in completed_events[0].output, (
            f"CompletedEvent.output ({completed_events[0].output!r}) should "
            f"contain the last TextEvent.content ({last_text.content!r})"
        )

        # Session status mirrors the legacy contract.
        from app.runtime.session import session_store
        sess = session_store().get(sid)
        assert sess is not None
        assert sess.status == "completed"

    def test_step_started_and_completed_around_agent(self, real_llm_preset):
        """Trace panel contract: every executed node emits a
        NodeStart/NodeEnd pair. The agno-native `Step(agent=...)`
        path still emits these — `_on_step_started` / `_on_step_completed`
        in EventAdapter are agnostic to whether the step ran via
        `executor=` or `agent=`."""
        from app.core.events import NodeEndEvent, NodeStartEvent
        from app.core.compile import run_leg

        nodes = [{
            "id": "ag",
            "type": "agent",
            "position": {"x": 0, "y": 0},
            "data": {"label": "Bot", "config": {}},
        }]
        _, events, _wf = run_leg(workflow_id="wf-native-2", name="wf-native-2", db_nodes=nodes, db_edges=[], input="x")

        starts = [e for e in events if isinstance(e, NodeStartEvent)]
        ends = [e for e in events if isinstance(e, NodeEndEvent)]
        assert len(starts) == 1
        assert starts[0].nodeId == "ag"
        assert starts[0].nodeType == "agent"
        assert len(ends) == 1
        assert ends[0].nodeId == "ag"
        assert ends[0].status == "ok"

# ─────────────────────────────────────────────────────────────────
# 2. Error path — no resolvable model returns a single ErrorEvent
# ─────────────────────────────────────────────────────────────────
class TestAgentNativeError:
    def test_no_model_emits_single_error(self, monkeypatch):
        """No default preset is seeded, and the agent's inline config
        is empty. `_build_agent_for_node` raises `_AgentBuildError`
        with the same user-facing message the legacy handler used,
        and the orchestrator wraps it in a synthetic-error Step that
        EventAdapter translates to a single canonical ErrorEvent.

        This matches the legacy contract that the front-end error
        trace panel depends on (`len(errs) == 1`).
        """
        from app.core.compile import run_leg
        import app.core.llm_runner as lr

        # Force "no default preset" by monkeypatching the resolver.
        monkeypatch.setattr(
            lr, "_resolve_default_preset_id",
            lambda db=None, user_id=None: None,
        )

        nodes = [{
            "id": "ag",
            "type": "agent",
            "position": {"x": 0, "y": 0},
            "data": {"label": "Bot", "config": {"model": {}}},
        }]
        _, events, _wf = run_leg(workflow_id="wf-no-model", name="wf-no-model", db_nodes=nodes, db_edges=[], input="x")

        errs = [e for e in events if isinstance(e, ErrorEvent)]
        # Exactly one ErrorEvent — matches `test_no_handler_node_emits_only_error_event`.
        assert len(errs) == 1
        msg = errs[0].message
        # Legacy wording is preserved verbatim so existing front-end
        # error panels and tests that substring-match on it keep working.
        assert "Agent 'Bot' has no model" in msg
        assert "default LLM preset" in msg

    def test_legacy_agent_handler_no_longer_importable(self):
        """Pin: the `_agent_handler` symbol is gone. The agent-strategy
        fold deletes the legacy registry entirely; this test
        catches anyone who tries to resurrect it as a back-compat
        shim."""
        # After the agent-strategy fold, the agent strategy
        # owns `build()` + `to_source()` inline — the legacy
        # `compile.emitters.agent` module is deleted. The new home
        # for the agent implementation is `app.core.strategies.agent`;
        # confirm that module does NOT expose a legacy `_agent_handler`.
        import app.core.strategies.agent as agent_strat
        assert not hasattr(agent_strat, "_agent_handler"), (
            "agent strategy must not expose a legacy handler symbol"
        )

# ─────────────────────────────────────────────────────────────────
# 3. Export parity — generator emits the same Step(agent=Agent(...))
# shape that the runtime now consumes.
# ─────────────────────────────────────────────────────────────────
class TestExportParity:
    def test_exported_python_uses_agent_native_shape(self):
        """The generator emits `Step(name=..., agent=...)` (see
        `core/generator/emitters/agent.py:step_block`); the runtime
        fold brought the in-process runtime onto the same shape, so
        exported Python and the in-process runtime now agree on
        the exact same construction path. Render a tiny workflow
        and assert the generated source contains
        `Step(name=..., agent=` for the agent node.
        """
        from app.core.compile import to_python_source as render_python

        nodes = [{
            "id": "ag",
            "type": "agent",
            "position": {"x": 0, "y": 0},
            "data": {
                "label": "Bot",
                "config": {
                    "model": {
                        "provider": "anthropic",
                        "modelId": "claude-sonnet-4-5",
                        "apiKey": "sk-placeholder",
                    },
                    "instructions": "be helpful",
                },
            },
        }]
        edges = []
        source = render_python({"name": "Parity", "nodes": nodes, "edges": edges})

        # The runtime contract: an Agent step is created via
        # `Step(name=<label>, agent=<nid>_agent)`. Same shape on both
        # sides — proves the export and runtime paths agree.
        assert "ag_agent = Agent(" in source, (
            "exported Python should construct Agent(...) — generator "
            "must produce agno-native shape so exported workflows can "
            "be hand-edited and re-run via `python workflow.py`."
        )
        assert "Step(name=" in source and "agent=ag_agent)" in source
