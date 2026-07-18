"""Explicit runtime/export parity assertions.

Per `docs/architecture/SINGLE_ENGINE_PLAN.md`:

  > 把"隐式覆盖"提升为"显式断言",防止未来 emitter 双 API 漂移。

Every built-in template is exercised twice:

  - **Path A** (in-process): `compile.build_workflow(...)` →
    `Wf.run(stream=True, ...)` → `EventAdapter.adapt(...)`.
  - **Path B** (exported source): `serialize.to_python_source(...)` →
    `exec(...)` into a module → `mod.workflow.run(stream=True, ...)` →
    `EventAdapter.adapt(...)`.

The two paths must produce the **same event-type sequence**
(`[type(e).__name__ for e in events]`). We do NOT compare payloads
(text content, durations, timestamps) — LLM output is non-deterministic
and the runtime + the export share the same `CompileCtx`, so the
*shape* of the stream is what proves they agree.

Templates covered: 5 EN + 5 ZH = 10 built-ins. For the HITL
template (`tpl-ask-the-user*`) the first leg pauses; we drive the
resume leg by stubbing `user_input` on the captured
`StepRequirement` before calling `Wf.continue_run(...)`.

Why the resume stub is OK:
  - Both paths use the same `Wf.continue_run` underneath — the test
    only varies the *value* of `user_input`, not the orchestration
    code path. If the resume stub produces a different event sequence
    between path A and path B, something has drifted; the test catches it.
  - The LLM stub (`seeded_default_preset`) is symmetric: it patches
    `Claude.invoke_stream` regardless of whether the model instance
    was built by the runtime's `_build_agent_for_node` or by the
    export's literal `Claude(...)` constructor.

What this test does NOT cover:
  - Tool-source node parity for the HTTP wrapper (the agent's stub
    doesn't actually call tools, so tool wiring differences wouldn't
    surface here — they show up in `tests/test_tool_wiring.py`).
  - Wire-format parity for the SSE layer (that's
    `tests/test_runtime_api.py`).
  - The full LLM round-trip parity (uses the deterministic echo stub).
"""
from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Any

import pytest

# `seeded_default_preset` lives in `conftest.py` — it stubs
# `Claude.invoke_stream` so the agent's "LLM call" returns a canned
# `[label] echo: input` payload regardless of whether the Agent was
# built by the runtime's `_build_agent_for_node` or by the export's
# `Claude(...)` literal.
pytestmark = pytest.mark.usefixtures("seeded_default_preset")

def _verify_default_preset_resolves() -> None:
    """Sanity check the LLM preset fixture before running parity.

    When `seeded_default_preset` doesn't seed properly the runtime
    returns the canonical "Agent has no model" CompileError and the
    parity suite turns into a single ErrorEvent vs N-event comparison
    that's not informative. Catch it here with a clear message.
    """
    import app.core.llm_runner as lr
    pid = lr._resolve_default_preset_id()
    if not pid:
        raise RuntimeError(
            "seeded_default_preset fixture did not register a default "
            "LLM preset — parity suite can't run. Make sure the "
            "fixture's monkeypatch ran (it requires the `db` fixture)."
        )

TEMPLATES_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "app" / "templates" / "workflows"
)

# ─────────────────────────────────────────────────────────────────
# Template inventory — single source of truth for which templates
# the parity suite covers. Adding a new built-in template is a
# two-line change: drop the JSON into `templates/workflows/` and add
# its id here. The parametrize below fans out automatically.
# ─────────────────────────────────────────────────────────────────
EN_TEMPLATE_IDS = [
    "tpl-ask-the-user",
    "tpl-conditional-greeting",
    "tpl-hello-world",
    "tpl-iterative-story",
    "tpl-parallel-summary",
]
ZH_TEMPLATE_IDS = [f"{tid}-zh-CN" for tid in EN_TEMPLATE_IDS]
ALL_TEMPLATE_IDS = EN_TEMPLATE_IDS + ZH_TEMPLATE_IDS

# Templates whose runtime/export parity is broken today.
#
# Router LLM removal (finalised): the Router LLM picker was
# removed. All built-in templates now pass parity without xfail.
#
# Templates-5 cleanup: the 13-template gallery was reduced to 5 focused
# examples (`tpl-hello-world` / `tpl-ask-the-user` /
# `tpl-conditional-greeting` / `tpl-iterative-story` /
# `tpl-parallel-summary`). Router / parallel / compound variants
# were deleted as duplicates or info-passing bloat; the remaining 5
# cover one concept each and all pass parity.
#
# Add new entries here ONLY when a template's runtime/export event
# sequence diverges for a reason that the fix-it-this-PR work can't
# resolve. Each entry should link to a tracking issue.
PARITY_XFAIL_IDS: set[str] = set()

def _load_template_body(template_id: str) -> dict:
    """Read a built-in template JSON and return the inner workflow body.

    The shape returned here is what `to_python_source` consumes —
    `{name, nodes, edges}`. The full envelope (`schemaVersion`,
    `kind`, `exportedAt`) is dropped; the parity check is purely
    about graph + emitter behaviour, not file-format versioning.

    We also stamp the default LLM preset's `apiKey` (and `baseUrl` when
    set) onto every agent node's `config.model` — the template JSONs
    deliberately omit secrets (they're shipped to the repo), but the
    runtime's `build_model(...)` refuses to construct a model without
    a key. Symmetric for both paths: path B uses the export's literal
    `Claude(id=...)` (no api_key, but the stub `Claude.invoke_stream`
    bypasses the network so construction is enough), while path A goes
    through `build_model` which is the canonical gatekeeper.
    """
    p = TEMPLATES_DIR / f"{template_id}.json"
    if not p.exists():
        raise FileNotFoundError(f"template {template_id!r} not found at {p}")
    payload = json.loads(p.read_text(encoding="utf-8"))
    body = payload["envelope"]["workflow"]
    workflow_body = {
        "name": body["name"],
        "nodes": [dict(n) for n in body["nodes"]],
        "edges": [dict(e) for e in body["edges"]],
    }
    _stamp_default_api_key(workflow_body["nodes"])
    return workflow_body

def _stamp_default_api_key(nodes: list[dict]) -> None:
    """Mutate agent nodes so `config.model` carries the default
    preset's `apiKey` (and `baseUrl`).

    Templates ship WITHOUT secrets on purpose — they live in the
    user's `LlmPreset` table. The runtime + export paths handle a
    secret-less model config very differently:

      - Path A (`compile.build_workflow(...)` → `agent.build(...)` →
        `llm_runner.build_model(...)`): refuses to build if
        `apiKey` is empty (returns None → CompileError).
      - Path B (`serialize.to_python_source(...)`): emits
        `Claude(id='...')` literally, no api_key. Construction
        succeeds; the conftest's `Claude.invoke_stream` stub
        bypasses the actual network call.

    To make both paths agree we inject the preset's api_key inline.
    The `seeded_default_preset` fixture is responsible for resolving
    the default preset.
    """
    import app.core.llm_runner as lr

    pid = lr._resolve_default_preset_id()
    if not pid:
        # The fixture isn't active — let the rest of the test surface
        # the canonical "Agent has no model" error so failures are
        # still informative.
        return
    preset = lr._resolve_preset(pid)
    if not preset:
        return
    api_key = preset.get("api_key") or ""
    base_url = preset.get("base_url") or None
    for node in nodes:
        if node.get("type") != "agent":
            continue
        cfg = node.get("data", {}).get("config") or {}
        model = cfg.get("model") or {}
        if not model:
            continue
        # Skip if already has a key — user-supplied config wins.
        if model.get("apiKey") or model.get("presetId"):
            continue
        model["apiKey"] = api_key
        if base_url and not model.get("baseUrl"):
            model["baseUrl"] = base_url
        cfg["model"] = model

# ─────────────────────────────────────────────────────────────────
# Path A — in-process runtime
# ─────────────────────────────────────────────────────────────────
def _run_path_a(template_id: str) -> list[str]:
    """Compile → run → adapt; return a list of event class names.

    For HITL templates we capture the resume leg too, by stubbing
    `user_input` on the captured `StepRequirement` and calling
    `compile.continue_leg(...)` — same path the runtime service uses.
    """
    from app.core.compile import continue_leg, run_leg
    from app.core.events import ConfirmationEvent
    from app.runtime.session import session_store

    wf_body = _load_template_body(template_id)
    sid, events, wf = run_leg(
        workflow_id=f"parity-a-{template_id}",
        name=f"parity-a-{template_id}",
        db_nodes=wf_body["nodes"],
        db_edges=wf_body["edges"],
        input="parity",
    )

    # HITL resume leg: stub user_input on the captured requirement
    # so both paths drive the same `Wf.continue_run(...)` with the
    # same payload. The values are deterministic — they don't need
    # to be "right", only to be the same in path A and path B.
    if any(isinstance(e, ConfirmationEvent) for e in events):
        # Multi-user: resume bookkeeping lives on the
        # session itself, not on a module-level dict keyed by sid.
        sess = session_store().get(sid)
        if sess is None:
            raise AssertionError(
                f"path A: no RuntimeSession for {sid!r} after pause leg"
            )
        reqs = sess.get_last_step_requirements()
        _stub_user_input_on_requirements(reqs)
        run_id = sess.get_last_run_id()
        if not run_id:
            raise AssertionError(
                f"path A: no run_id captured for {template_id!r} after pause leg"
            )
        _, resume_events = continue_leg(
            wf,
            session_id=sid,
            run_id=run_id,
            step_requirements=reqs,
        )
        events = events + resume_events

    return [type(e).__name__ for e in events]

# ─────────────────────────────────────────────────────────────────
# Path B — export + exec + run
# ─────────────────────────────────────────────────────────────────
def _run_path_b(template_id: str) -> list[str]:
    """Render → exec → run; return a list of event class names.

    The export produces a `<safe_name>.py` that declares
    `workflow = Workflow(name=..., steps=_steps)` at module scope.
    We `exec()` the source into a fresh module, attach a SQLite db +
    `cache_session=True` (the export omits both so the file stays
    self-contained — for parity we restore them so resume works),
    then drive `mod.workflow.run(...)` and adapt with the same
    EventAdapter path A uses.

    For HITL templates the first leg pauses with a `ConfirmationEvent`;
    we read the captured `run_id` + `step_requirements` out of
    `event_adapter`'s module-level side tables (populated by the
    adapter during the stream), stub `user_input`, and call
    `mod.workflow.continue_run(...)` directly.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool
    from agno.db.sqlite import SqliteDb

    from app.core.compile.run import extract_node_types
    from app.core.compile.serialize import to_python_source
    from app.core.event_adapter import EventAdapter
    from app.core.events import ConfirmationEvent
    from app.runtime.session import RuntimeSession, session_store

    wf_body = _load_template_body(template_id)
    code = to_python_source(wf_body, name=template_id)

    # Exec the export into a private module — collisions across
    # parametrized runs would leak global names otherwise.
    mod = types.ModuleType(f"parity_b_{template_id}")
    exec(compile(code, f"<parity-b-{template_id}>", "exec"), mod.__dict__)
    if not hasattr(mod, "workflow"):
        raise AssertionError(
            f"export for {template_id!r} produced no `workflow` symbol — "
            "the generator must always emit `workflow = Workflow(...)`"
        )

    # The export omits `db=` and `cache_session=True` to keep the
    # generated file self-contained. For parity testing the resume
    # leg needs both — restore them on the exec'd workflow.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    mod.workflow.db = SqliteDb(db_engine=engine)
    mod.workflow.cache_session = True

    # Mirror path A's runtime session bookkeeping so EventAdapter
    # can resolve node_types for the trace panel.
    sid = f"parity-b-{template_id}"
    sess = session_store().get(sid)
    if sess is None:
        import time as _time
        sess = session_store().create(
            workflow_id=f"parity-b-{template_id}",
            input="parity",
            user_id=None,
            session_id=sid,
        )
        sess.wf = mod.workflow
        sess.started_at = _time.monotonic()
    sess.node_types = extract_node_types(mod.workflow)

    adapter = EventAdapter(session_id=sid)
    try:
        agno_events = mod.workflow.run(
            input="parity",
            stream=True,
            stream_events=True,
            stream_executor_events=True,
            session_id=sid,
        )
        events = adapter.adapt(agno_events)
    except Exception as e:  # noqa: BLE001
        raise AssertionError(
            f"path B first leg failed for {template_id!r}: "
            f"{type(e).__name__}: {e}"
        )

    if any(isinstance(e, ConfirmationEvent) for e in events):
        # Multi-user: resume bookkeeping lives on the
        # session itself, not on a module-level dict keyed by sid.
        sess = session_store().get(sid)
        if sess is None:
            raise AssertionError(
                f"path B: no RuntimeSession for {sid!r} after pause leg"
            )
        reqs = sess.get_last_step_requirements()
        _stub_user_input_on_requirements(reqs)
        run_id = sess.get_last_run_id()
        if not run_id:
            raise AssertionError(
                f"path B: no run_id captured for {template_id!r} after pause leg"
            )
        try:
            agno_events_2 = mod.workflow.continue_run(
                run_id=run_id,
                session_id=sid,
                step_requirements=reqs,
                stream=True,
                stream_events=True,
                stream_executor_events=True,
            )
            events = events + adapter.adapt(agno_events_2)
        except Exception as e:  # noqa: BLE001
            raise AssertionError(
                f"path B resume leg failed for {template_id!r}: "
                f"{type(e).__name__}: {e}"
            )

    return [type(e).__name__ for e in events]

# ─────────────────────────────────────────────────────────────────
# Resume helper
# ─────────────────────────────────────────────────────────────────
def _stub_user_input_on_requirements(reqs: list[Any]) -> None:
    """Mutate the first user-input requirement in place.

    Maps the schema field name onto a deterministic stub value:

      - `selection` (HITL choice) → `"approve"` (the first choice
        of `tpl-ask-the-user`'s select-style ask prompts).
      - `confirmation` (HITL confirm) → `True`.
      - `response` (HITL text) → `"ok"`.

    Two surfaces need to be set:
      - `req.user_input = {...}` — picked up by `Wf.continue_run`'s
        `user_input_data = _ar.user_input` line and forwarded into
        `step_input.user_input` so the executor stub (`_echo_user_input`)
        can read it.
      - `req.user_input_schema[i].value = <v>` — needed for
        `StepRequirement.is_resolved` to return True. agno 2.8.7's
        resolver iterates the schema and checks `field.value is None`
        for each `required` field; setting only `req.user_input` is
        not enough.

    In-place mutation is what agno 2.8.7 expects — `Wf.continue_run`
    reads off the same objects, not copies.
    """
    for req in reqs or []:
        if not getattr(req, "requires_user_input", False):
            continue
        schema = getattr(req, "user_input_schema", None) or []
        field_name = "response"
        if schema:
            first = schema[0]
            if isinstance(first, dict):
                field_name = first.get("name") or field_name
            else:
                field_name = getattr(first, "name", None) or field_name
        if field_name == "selection":
            stub_value = "approve"
        elif field_name == "confirmation":
            stub_value = True
        else:
            stub_value = "ok"
        # 1) Set on the schema field (drives `is_resolved`).
        for f in schema:
            if isinstance(f, dict):
                continue  # plain dicts have no `.value` setter
            fname = getattr(f, "name", None)
            if fname == field_name:
                f.value = stub_value
                break
        # 2) Set on the requirement's user_input dict (drives the
        #    executor's `_echo_user_input` when the leg resumes).
        req.user_input = {field_name: stub_value}
        break

# ─────────────────────────────────────────────────────────────────
# Parametrized parity assertion
# ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("template_id", ALL_TEMPLATE_IDS)
def test_runtime_export_event_sequence_parity(
    template_id: str,
) -> None:
    """Path A's event-type sequence must equal path B's, for every
    built-in template (EN + ZH).

    Failure shape: prints both sequences side-by-side so the diff
    points at the first divergence.
    """
    _verify_default_preset_resolves()
    types_a = _run_path_a(template_id)
    types_b = _run_path_b(template_id)
    if types_a != types_b and len(types_a) == 1 and types_a[0] == "ErrorEvent":
        # Surface path A's error message so the failure points at
        # the underlying cause (compile error vs. agno internal).
        from app.core.events import ErrorEvent
        from app.core.compile import run_leg as _rl
        wf_body = _load_template_body(template_id)
        _, events_a, _ = _rl(
            workflow_id=f"diag-a-{template_id}",
            name=f"diag-a-{template_id}",
            db_nodes=wf_body["nodes"],
            db_edges=wf_body["edges"],
            input="parity",
        )
        err_msg = ""
        for e in events_a:
            if isinstance(e, ErrorEvent):
                err_msg = e.message
                break
        if err_msg:
            print(f"\n[diag] path A error for {template_id!r}: {err_msg}")

    # Templates whose runtime/export event sequence still diverges today
    # are tracked in `PARITY_XFAIL_IDS` above (phase.1 emptied the set
    # once the Router LLM picker was removed — all 26 templates now pass
    # parity without xfail).
    if types_a != types_b and template_id in PARITY_XFAIL_IDS:
        pytest.xfail(
            f"runtime/export parity drift for {template_id!r} — "
            f"see PARITY_XFAIL_IDS in test_compile_runtime_parity.py"
        )

    assert types_a == types_b, (
        f"\nruntime/export event-sequence divergence for {template_id!r}\n"
        f"  path A (in-process runtime): {types_a}\n"
        f"  path B (exported .py):       {types_b}\n"
        f"  first divergent index: "
        f"{next((i for i, (a, b) in enumerate(zip(types_a, types_b)) if a != b), 'len mismatch')}\n"
    )

# ─────────────────────────────────────────────────────────────────
# row B  — drive_leg_with_trace facade parity
# ─────────────────────────────────────────────────────────────────
# `drive_leg_with_trace` is the shared leg facade used by both
# `chat_builder_run` (Pillar 3, debug tool) and any future trace-aware
# runner. Pillar 1's runtime path (`runtime_service`) keeps going
# through `run_leg` directly — it doesn't need per-step accumulation,
# it streams events over SSE. This suite locks the facade's return
# contract so neither caller can regress the other silently.
class TestDriveLegWithTraceFacade:
    """`drive_leg_with_trace` return-shape + integration parity.

    Each test exercises the facade against a built-in template
    (`tpl-hello-world`, the simplest non-HITL template — the rest of
    the parity suite would also work but adds runtime cost without
    extra coverage for the facade contract).
    """

    def _build(self, template_id: str):
        """Compile the template once; return `(wf, session_id)`."""
        from app.core.compile import build_workflow
        from app.runtime.session import session_store

        wf_body = _load_template_body(template_id)
        sid = f"facade-{template_id}"
        wf = build_workflow(
            workflow_id=f"facade-{template_id}",
            name=f"facade-{template_id}",
            db_nodes=wf_body["nodes"],
            db_edges=wf_body["edges"],
            session_id=sid,
        )
        # `drive_leg_with_trace` uses `EventAdapter(session_id=...)`
        # which consults `session_store` for status / node_types.
        # Synthesize a row so the adapter doesn't 404. Mirrors what
        # `runtime_service` does before invoking `run_leg`.
        from app.runtime.session import session_store as _store
        sess = _store().get(sid)
        if sess is None:
            sess = _store().create(
                workflow_id=f"facade-{template_id}",
                input="parity",
                user_id=None,
                session_id=sid,
            )
            sess.wf = wf
        # Keep the linter quiet about the `store` alias above.
        del session_store
        return wf, sid

    def test_return_shape_is_seven_tuple(self) -> None:
        """The facade must return exactly 7 values in the documented
        order: `(session_id, events, steps, pending_requirements,
        output, error, status)`.

        Earlier this returned 6 values (no `status`) — chat-builder
        had to derive it from terminal events, and `events` was a
        stub `[]`. The 7-tuple + populated events landed in the same
        commit as the facade (`chat_builder_run.run_workflow` was
        the only consumer; its unpacking was wrong in the first
        revision, hence the explicit shape pin).
        """
        from app.core.compile import drive_leg_with_trace, LegStep

        _verify_default_preset_resolves()
        wf, sid = self._build("tpl-hello-world")
        out = drive_leg_with_trace(
            wf,
            session_id=sid,
            input="hello",
            timeout_sec=30.0,
        )
        assert isinstance(out, tuple) and len(out) == 7, (
            f"expected 7-tuple, got {len(out)}-tuple: {out!r}"
        )
        run_sid, events, steps, pending, output, error, status = out
        assert run_sid == sid
        assert isinstance(events, list)
        assert isinstance(steps, list)
        assert all(isinstance(s, LegStep) for s in steps)
        assert isinstance(pending, list)
        assert isinstance(status, str) and status in {
            "completed", "failed", "paused",
        }
        # `output` / `error` are `Optional[str]`; we don't pin
        # either value — the canned LLM echo may produce empty text.
        assert error is None or isinstance(error, str)
        assert output is None or isinstance(output, str)

    def test_events_list_is_populated(self) -> None:
        """Lock the regression where the facade returned `[]` for
        events even though the docstring advertised it. Tests that
        only checked `len(out) == 6` (the old shape) didn't notice
        the gap; once we added `status` to the tuple, populating
        events became the natural thing to do — tests stay honest."""
        from app.core.compile import drive_leg_with_trace

        _verify_default_preset_resolves()
        wf, sid = self._build("tpl-hello-world")
        _sid, events, _steps, _pending, _output, _error, _status = (
            drive_leg_with_trace(
                wf, session_id=sid, input="hello", timeout_sec=30.0,
            )
        )
        # `tpl-hello-world` runs ONE Step with one Agent invocation.
        # At minimum we expect NodeStart + NodeEnd + Completed (+ a
        # TextEvent in the canned echo). 3+ events is a safe lower
        # bound without pinning exact stream contents.
        assert len(events) >= 3, (
            f"events list should be populated for a real workflow; "
            f"got {events!r}"
        )

    def test_status_matches_terminal_event(self) -> None:
        """The facade's `status` field must agree with whichever
        terminal event is in `events` — the same rule
        `chat_builder_run._collect_leg` used to apply locally. A
        CompletedEvent → 'completed'; a ConfirmationEvent → 'paused'
        (HITL); an ErrorEvent → 'failed'; otherwise derived from
        per-step ok/error counts."""
        from app.core.compile import drive_leg_with_trace
        from app.core.events import (
            CompletedEvent, ConfirmationEvent, ErrorEvent,
        )

        _verify_default_preset_resolves()
        wf, sid = self._build("tpl-hello-world")
        _sid, events, _steps, _pending, _output, _error, status = (
            drive_leg_with_trace(
                wf, session_id=sid, input="hello", timeout_sec=30.0,
            )
        )
        # `tpl-hello-world` is non-HITL; expect 'completed'.
        if any(isinstance(e, CompletedEvent) for e in events):
            assert status == "completed"
        elif any(isinstance(e, ConfirmationEvent) for e in events):
            assert status == "paused"
        elif any(isinstance(e, ErrorEvent) for e in events):
            assert status == "failed"
        else:
            # No terminal event — the facade falls back to
            # `completed` if any step ended ok, else `failed`.
            # The hello-world template always completes a step,
            # so the branch below is the expected one.
            assert status == "completed"

    def test_chat_builder_run_folds_facade_into_run_trace(self) -> None:
        """`chat_builder_run.run_workflow` is the only caller of the
        facade today. This test pins the contract that its
        `RunTrace` fields are populated from the facade's 7-tuple
        — if the unpacking drifts (e.g. someone swaps
        `trace.status, _events, ... = facade(...)` for a 5-tuple),
        the trace will record the wrong fields and the JSON
        contract published by `inspect_run` will break."""
        from app.services import chat_builder_run

        _verify_default_preset_resolves()
        # Reset module singleton so this test doesn't see state
        # from a previous case in the same process.
        chat_builder_run.set_store(chat_builder_run.RunTraceStore())

        wf_body = _load_template_body("tpl-hello-world")
        trace = chat_builder_run.run_workflow(
            wf_body["nodes"],
            wf_body["edges"],
            workflow_id="facade-cb",
            workflow_name="facade-cb",
            input="hello",
            timeout_sec=30.0,
        )
        # All five trace-level fields must be populated. The
        # previous incarnation assigned `session_id` (the facade's
        # first return value) to `trace.status` because the
        # unpacking was 5-tuple; this assertion catches that class
        # of regression without needing to re-derive the full
        # contract.
        assert trace.status in {"completed", "failed", "paused"}, (
            f"trace.status is one of the three terminal states; "
            f"got {trace.status!r}"
        )
        assert isinstance(trace.steps, list)
        # `RunStep` is now `core.compile.LegStep` (alias); both
        # names should resolve to the same dataclass.
        from app.core.compile import LegStep
        assert all(isinstance(s, LegStep) for s in trace.steps)
        # Store round-trip: `inspect_run` should return the same
        # JSON shape we just folded.
        d = chat_builder_run.inspect_run(trace.run_id)
        assert d is not None
        assert d["status"] == trace.status
        assert d["run_id"] == trace.run_id
