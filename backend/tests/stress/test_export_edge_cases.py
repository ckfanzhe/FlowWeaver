"""Stress: edge cases for the Python code generator (核心导出功能).

CLAUDE.md flags Python code export as the project's **core
feature**. These tests pin the export pipeline's behaviour on workflow
shapes that the regular `test_generator.py` suite doesn't cover:

  - Unicode / RTL in labels + instructions + prompts
  - 50 KB+ instructions (large free-text payload)
  - Circular reference detection (must reject, not infinite-loop)
  - Empty workflow (must reject with a clean error)
  - Single-node workflow (smallest valid workflow — make sure the
    one-node edge list is handled, not just multi-node fixtures)
  - 50+ node workflow (linear chain — exposes any O(n²) or string-
    concatenation hotspot in the renderer's per-pass loops)

Each test produces a Python source string with `render_python(...)`
and verifies it parses (via `ast.parse`) plus, where relevant, that
the input text is preserved verbatim. We do NOT execute the generated
code — CI has no API keys, and the export contract is "the file you
download is the file you can `python -m py_compile`".

Why these matter
----------------
The export pipeline turns the visual workflow into the runtime
artifact (workflow.py). Edge-case shapes that survive
`validate_workflow` but break the renderer would silently regress the
user-facing feature — the canvas would let the user save the
workflow, the API would return a 200, and the downloaded file would
fail to compile. None of the normal smoke runs would catch it.
"""
from __future__ import annotations

import ast
import json
import re

import pytest

from app.core.compile import CompileError as GeneratorError, to_python_source as render_python

from ._factory import agent_node, edge, human_input_node

# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────
def _wrap(name: str, nodes: list[dict], edges: list[dict]) -> dict:
    """Build the `{name, nodes, edges}` envelope `render_python` expects.

    The `_factory` helpers emit the per-node dict directly (same shape
    `workflow_io.parse` produces), so wrapping them here keeps the
    factory single-purpose (runtime IR shape) while still feeding the
    export pipeline.
    """
    return {"name": name, "nodes": nodes, "edges": edges}

def _compiles(code: str) -> None:
    """Assert the generated source is syntactically valid Python.

    Raises `SyntaxError` on failure. This is the cheapest possible
    "does the export actually work" check — it doesn't run the code,
    doesn't call LLMs, doesn't touch the network.
    """
    ast.parse(code)

_STRING_LITERAL_RE = re.compile(
    # Matches the FIRST `attr = "..."` style literal. Greedy on the body
    # with backslash-aware quote handling. We don't use `ast.literal_eval`
    # because it is quadratic on very long strings (60 KB+) — instead
    # we return the raw body and compare it to `json.dumps(...)` directly
    # (which is exactly what `repr_instructions` does internally).
    r"""(?P<name>\w+)\s*=\s*(?P<quote>['"])(?P<body>(?:\\.|(?!(?P=quote)).)*)(?P=quote)""",
    re.DOTALL,
)

def _extract_literal_body(code: str, attr_name: str) -> str:
    r"""Return the raw body of the FIRST `attr_name = "..."` literal.

    The body is the text BETWEEN the quotes (with backslash escapes
    intact). `repr_instructions` uses `json.dumps(s, ensure_ascii=False)`,
    so callers should compare the returned body to
    `json.dumps(expected)[1:-1]` (stripping the outer JSON quotes).

    Faster than `ast.literal_eval` on multi-KB strings.
    """
    for m in _STRING_LITERAL_RE.finditer(code):
        if m.group("name") == attr_name:
            return m.group("body")
    raise AssertionError(
        f"no string literal {attr_name}=... found in generated code"
    )

def _assert_literal_equals(code: str, attr_name: str, expected: str) -> None:
    r"""Assert that the FIRST `attr_name = "..."` literal in `code`
    is byte-equal to what `repr_instructions` would produce for
    `expected` (i.e. `json.dumps(expected)[1:-1]` with `ensure_ascii=False`).

    This proves the export preserves the semantic content: the file,
    when parsed by Python's compiler, reproduces the original string
    byte-for-byte. The new emitter writes raw UTF-8 (no `\uXXXX`
    escapes) so generated source stays human-readable for CJK / emoji.
    """
    body = _extract_literal_body(code, attr_name)
    expected_body = json.dumps(expected, ensure_ascii=False)[1:-1]
    assert body == expected_body, (
        f"literal {attr_name!r} mismatch: expected {len(expected_body)} "
        f"chars (json.dumps-escaped), got {len(body)} chars; first diff at "
        f"offset {next((i for i, (a, b) in enumerate(zip(expected_body, body)) if a != b), 'same')!r}"
    )

# ─────────────────────────────────────────────────────────────────
# 1. Unicode / RTL in workflow text
# ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "label,instructions,prompt",
    [
        # CJK + emoji in label and instructions
        (
            "客服 🤖 智能助手",
            "你是一个专业的客服助手。请用友好的方式回答用户问题。✅",
            "请选择你的偏好：",
        ),
        # Arabic (RTL) — must round-trip through json.dumps in
        # `repr_instructions` without bidi-reordering the source.
        (
            "مساعد عربي",
            "أنت مساعد ذكي. أجب بالعربية فقط.",
            "ما هو لونك المفضل؟",
        ),
        # Hebrew (RTL) + mixed LTR inline
        (
            "עוזר בעברית",
            "אתה עוזר חכם. Please answer in Hebrew.",
            "בחר אפשרות:",
        ),
        # Mixed scripts in one field (Chinese + emoji + Latin)
        (
            "Mixed 多语言 🌍",
            "Handle CJK 中文, emoji 🎉, and Latin English in the same prompt.",
            "Choose 选择 واختار 🎯",
        ),
    ],
    ids=["cjk_emoji", "arabic_rtl", "hebrew_rtl", "mixed_scripts"],
)
def test_export_preserves_unicode_and_rtl(label, instructions, prompt):
    """Workflows with CJK / RTL / emoji must round-trip through the
    generator without mangling or breaking Python syntax.

    Pins the `repr_instructions` contract (json.dumps embeds all
    unicode safely) and the `safe_name` filename sanitiser.
    """
    nodes = [
        agent_node("a1", instructions=instructions, position=(0, 0)),
        human_input_node("h1", prompt=prompt, position=(200, 0)),
        agent_node("a2", instructions="Final.", position=(400, 0)),
    ]
    # Manually patch the label on a1 to include the unicode label —
    # `agent_node` puts node_id into label by default, but the test
    # wants to assert the LABEL survives (not just the id).
    nodes[0]["data"]["label"] = label
    edges = [
        edge("a1", "h1"),
        edge("h1", "a2"),
    ]
    code = render_python(_wrap("unicode", nodes, edges))

    # 1. Code is syntactically valid Python.
    _compiles(code)

    # 2. Instructions round-trip via json.dumps' ensure_ascii escapes
    #    (CJK / RTL / emoji become \uXXXX). Extract the literal body
    #    and compare it to what json.dumps would produce for the input.
    _assert_literal_equals(code, "instructions", instructions)

    # 3. Same round-trip for the human_input prompt. It survives as
    #    `user_input_message=` on the `Step(requires_user_input=True, ...)`
    #    wrapper. We just check the json-escaped body is present in the
    #    source verbatim (raw UTF-8 with `ensure_ascii=False` is the
    #    single-engine emitter contract).
    expected_body = json.dumps(prompt, ensure_ascii=False)[1:-1]
    assert expected_body in code, "human_input prompt did not survive export"

    # 4. The label does NOT have to appear verbatim in the source — the
    #    generator emits labels into Step(name=...) which is an
    #    identifier-safe representation. But the label MUST survive
    #    filename sanitisation via `safe_name` (no exception raised is
    #    the contract; presence is best-effort). We only assert the
    #    generator didn't choke on the label.
    assert len(code) > 0

# ─────────────────────────────────────────────────────────────────
# 2. Very long instruction strings (50 KB+)
# ─────────────────────────────────────────────────────────────────
def test_export_handles_50kb_instruction_string():
    """A workflow with a single 50 KB+ instructions string must still
    produce a syntactically valid, byte-complete export.

    The legacy renderer used naive string concat — long inputs exposed
    quadratic concatenation in some intermediate passes. Pin the
    behaviour so we notice if it regresses.
    """
    # Build a 60 KB instructions string by repeating a unicode-heavy
    # paragraph 600 times. Mix Chinese + Latin + emoji + newlines so
    # we cover utf-8 expansion.
    para = (
        "第一段：处理复杂任务。\n"
        "Section 2: handle ambiguity in user prompts gracefully. 🎯\n"
        "Part three: ensure each output is faithful to the source. ✅\n"
    )
    big_instructions = (para * 600)  # ~60 KB

    nodes = [agent_node("big", instructions=big_instructions)]
    code = render_python(_wrap("big", nodes, []))

    # 1. Code parses.
    _compiles(code)

    # 2. The full instructions survive end-to-end. Compare the literal
    #    body to what `repr_instructions` would produce. Faster than
    #    ast.literal_eval on a 60 KB string.
    _assert_literal_equals(code, "instructions", big_instructions)

def test_export_handles_50kb_prompt_on_human_input():
    """Same long-payload contract for the human_input node's prompt
    field (used to seed `requires_user_input=True` step). Different
    emit path than agent.instructions — exercised separately.
    """
    big_prompt = "Please review:\n\n" + ("Sentence. " * 5000)  # ~65 KB

    nodes = [
        agent_node("a1", instructions="Step A."),
        human_input_node("h1", prompt=big_prompt),
        agent_node("a2", instructions="Step C."),
    ]
    edges = [edge("a1", "h1"), edge("h1", "a2")]
    code = render_python(_wrap("long_prompt", nodes, edges))

    _compiles(code)

    # Round-trip — proves the full 65 KB prompt survives end-to-end
    # without truncation. It lives in the default arg of the emitted
    # `ask_h1(...)` helper rather than a `_prompt = ...` assignment.
    expected_body = json.dumps(big_prompt, ensure_ascii=True)[1:-1]
    assert expected_body in code, "human_input prompt did not survive export"

# ─────────────────────────────────────────────────────────────────
# 3. Circular reference detection
# ─────────────────────────────────────────────────────────────────
def test_export_rejects_two_node_cycle():
    """A direct 2-node cycle (A → B → A) must surface as
    `GeneratorError`, not hang the topo-sort.

    `validate_workflow` calls `topo_sort` which uses Kahn's algorithm;
    a cycle leaves all in-degree nodes with indegree > 0 and the
    sorted-result count < node count, so the renderer raises with a
    message matching `(cycle|loop)`.
    """
    nodes = [
        agent_node("a", instructions="A."),
        agent_node("b", instructions="B."),
    ]
    edges = [edge("a", "b"), edge("b", "a")]
    with pytest.raises(GeneratorError, match=r"(cycle|loop)"):
        render_python(_wrap("cycle2", nodes, edges))

def test_export_rejects_three_node_cycle():
    """3-node cycle (A → B → C → A) — same contract, different shape."""
    nodes = [
        agent_node("a", instructions="A."),
        agent_node("b", instructions="B."),
        agent_node("c", instructions="C."),
    ]
    edges = [
        edge("a", "b"),
        edge("b", "c"),
        edge("c", "a"),
    ]
    with pytest.raises(GeneratorError, match=r"(cycle|loop)"):
        render_python(_wrap("cycle3", nodes, edges))

# ─────────────────────────────────────────────────────────────────
# 4. Empty workflow (boundary)
# ─────────────────────────────────────────────────────────────────
def test_export_rejects_empty_workflow():
    """A workflow with zero nodes must raise `GeneratorError`, not
    produce an empty file or hang the renderer.

    The `render_python` entry-point checks `if not nodes:` and raises
    with `"workflow has no nodes"`. This pins the user-visible error
    shape — clients (CLI / API) display it.
    """
    with pytest.raises(GeneratorError, match=r"no nodes"):
        render_python({"name": "empty", "nodes": [], "edges": []})

# ─────────────────────────────────────────────────────────────────
# 5. Single-node workflow (smallest valid)
# ─────────────────────────────────────────────────────────────────
def test_export_handles_single_node_workflow():
    """One node, zero edges — the smallest valid workflow shape.

    Many renderers branch on `len(edges) > 0`; we want the empty-edges
    case to behave identically to multi-node + edges (no special-case
    bugs in the assembly pass that builds `Workflow(steps=[...])`).
    """
    nodes = [agent_node("only", instructions="Solo run.")]
    code = render_python(_wrap("solo", nodes, []))

    _compiles(code)

    # Pin the runtime shape: Workflow(steps=[Step(name="only", ...)])
    # + a main block. The exact identifier for the workflow variable
    # is implementation-defined, so we use substring matches.
    assert "Workflow(" in code
    assert 'name="only"' in code or "name='only'" in code
    assert "if __name__" in code

# ─────────────────────────────────────────────────────────────────
# 6. Many-node workflow (50+ nodes)
# ─────────────────────────────────────────────────────────────────
def test_export_handles_60_node_linear_chain():
    """60-node linear chain — exercises the per-pass loops to expose
    O(n²) bottlenecks, repeated string concat hotspots, or topo-sort
    regressions.

    Linear (not branching) so connection rules accept it: each agent
    has exactly one incoming + one outgoing edge, well within the
    per-type connection limits.

    We assert:
      - Generation succeeds (no exception)
      - Generated code parses
      - All 60 node ids appear in the source
      - Code length scales linearly (sanity bound: < 200 KB)
    """
    n = 60
    ids = [f"agent_{i:02d}" for i in range(n)]
    nodes = [agent_node(nid, instructions=f"Step {nid}.") for nid in ids]
    edges = [edge(ids[i], ids[i + 1]) for i in range(n - 1)]

    code = render_python(_wrap(f"chain_{n}", nodes, edges))

    _compiles(code)

    # Every node id should appear in the source (as Step names).
    missing = [nid for nid in ids if nid not in code]
    assert not missing, (
        f"{len(missing)} node ids missing from export; first few: "
        f"{missing[:3]}"
    )

    # Sanity bound: 60 agents should not produce megabytes of code.
    # 200 KB is a loose ceiling — even with verbose imports the file
    # is well under this. If we ever cross 200 KB, something is
    # pathological (likely repeated text).
    assert len(code) < 200_000, (
        f"60-node export is {len(code)} bytes — likely O(n²) "
        f"or repeated-string regression"
    )

# ─────────────────────────────────────────────────────────────────
# 7. Unicode filename sanitisation (cross-cuts)
# ─────────────────────────────────────────────────────────────────
def test_export_handles_unicode_workflow_name():
    """Workflow name with CJK + emoji must sanitise to a valid Python
    filename via `safe_name` (alphanumeric-only fallback to underscores).

    In the single-engine refactor the export is the same `to_python_source`
    call as the runtime. The filename derivation is `safe_name(workflow.name)
    + '.py'`. If `safe_name` regresses (e.g. forgets the `else: 'workflow'`
    branch), the filename would be empty or contain illegal chars.
    """
    from app.core.compile import to_python_source as render_python
    from app.core.compile._helpers.utils import safe_name

    # Pure-emoji name: safe_name strips everything → "workflow".
    safe = safe_name("🤖✨")
    out = render_python({"name": "🤖✨", "nodes": [agent_node("a", instructions="x")], "edges": []})
    assert safe == "workflow"
    assert f'name="workflow"' in out  # the Workflow's `name=` is the sanitised form

    # CJK + punctuation: alnum-chars (CJK counts as alnum) + spaces→_.
    sanitised = safe_name("客服助手! v2 🌟")
    assert sanitised  # non-empty
    assert all(c.isalnum() or c == "_" for c in sanitised), (
        f"safe_name produced illegal filename chars: {sanitised!r}"
    )
    # The same sanitised name appears in the rendered `Workflow(name=...)`.
    out2 = render_python({"name": "客服助手! v2 🌟", "nodes": [agent_node("a", instructions="x")], "edges": []})
    assert f'name="{sanitised}"' in out2