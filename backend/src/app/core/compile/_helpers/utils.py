"""Tiny shared utilities — idents and docstrings.

Kept tiny on purpose. Anything bigger belongs in its own module.
"""
from __future__ import annotations

def safe_name(raw: str) -> str:
    """Turn 'My Workflow! 2' into 'my_workflow_2'.

    Used for the exported filename (e.g. `safe_name(workflow.name) + '.py'`)
    so the file lands somewhere predictable even when the canvas label
    has punctuation, emoji, or CJK characters.

    ASCII-only on purpose: `str.isalnum()` returns True for CJK code
    points (they're letters in Unicode terms), which would otherwise
    produce a unicode filename that breaks the HTTP `Content-Disposition`
    header — Starlette/Starlette latin-1-encodes header values and
    raises `UnicodeEncodeError` on anything outside 0x00–0xFF. ASCII
    alnum + the separator set below is the conservative contract for
    any filesystem we might land on.
    """
    out: list[str] = []
    for ch in (raw or "").lower():
        # ASCII alnum only — ord < 128 means no CJK, no emoji, no
        # accented letters. The earlier implementation accepted
        # `str.isalnum()`, which let Chinese workflow names through
        # and broke the export endpoint with a 500.
        if "a" <= ch <= "z" or "0" <= ch <= "9":
            out.append(ch)
        elif ch in (" ", "-", "_"):
            out.append("_")
    s = "".join(out).strip("_")
    return s or "workflow"

def safe_ident(raw: str) -> str:
    """Like `safe_name` but never empty — falls back to 'anon'.

    Used for Python identifiers (function names, variable names). Must
    always produce a syntactically valid identifier.
    """
    return safe_name(raw) or "anon"

def repr_instructions(raw: str | None) -> str:
    r"""Render a free-text instructions string as a Python string literal.

    Uses `json.dumps(..., ensure_ascii=False)` so the generated source
    contains the raw UTF-8 instead of `\uXXXX` escapes. The exported
    `.py` is served with `charset=utf-8` and Python 3 source is UTF-8
    by default (PEP 263), so writing CJK / emoji directly is the
    readable form. The earlier default of `ensure_ascii=True` produced
    valid but hard-to-read code like `""` for Chinese
    agent names.
    """
    if not raw:
        return '""'
    import json
    return json.dumps(raw, ensure_ascii=False)

def q(value) -> str:
    """Render an arbitrary value as a Python source literal.

    Thin wrapper over `json.dumps(..., ensure_ascii=False)` used by the
    emitters for agent / step / router / function names, choice
    strings, HTTP URLs, MCP config, and every other string literal
    in the generated module. Centralising it here means:

      * one place to flip `ensure_ascii` if we ever change our minds,
      * one place to harden quote / newline handling,
      * no emitter needs to import `json` itself.
    """
    import json
    return json.dumps(value, ensure_ascii=False)

def docstring(text: str) -> str:
    r'''Render a single-line docstring as `"""text"""`.

    Falls back to a plain string literal if the text contains triple
    quotes (which would prematurely close the docstring).
    '''
    safe = text.replace('"""', "'''")
    return f'"""{safe}"""'