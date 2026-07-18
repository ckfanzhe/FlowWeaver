"""Real LLM invocation for the agent handler.

Builds an agno Model instance from a node's `model` config and calls it
synchronously. `build_model` is now **pure**: it reads only the preset
row or the inline `model` config on the node — no environment-variable
fallback. The platform's LLM configuration is the system-managed
`LlmPreset` table; the `.env.llm` file is a test-only concern.

agent : agent nodes run via agno's native
`Step(agent=Agent(...))` path; there is no longer an "agent handler" to
swap out. Tests that need a deterministic agent response monkeypatch
`Claude.invoke_stream` directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional

from app.db.models import LlmPreset
from app.db.session import session_scope

# ─────────────────────────────────────────────────────────────────
# Result type — text + token counts (for the trace panel)
# ─────────────────────────────────────────────────────────────────
@dataclass
class ModelResult:
    """What an LLM call returns. `text` is the assistant's reply;
    `tokens` is the input/output/total token counts (or None when agno
    didn't report them — e.g. auth failure, echo fallback)."""
    text: str
    tokens: Optional[dict[str, int]] = None

def _extract_tokens(out) -> dict[str, int] | None:
    """Read input/output/total from an agno `RunOutput.metrics` object.

    Returns None when the field is missing or all zero (e.g. an echo
    fallback that never reached the model). Callers should treat None
    as "unknown" and not surface it as a meaningful number.
    """
    metrics = getattr(out, "metrics", None)
    if metrics is None:
        return None
    try:
        inp = int(getattr(metrics, "input_tokens", 0) or 0)
        outp = int(getattr(metrics, "output_tokens", 0) or 0)
        tot = int(getattr(metrics, "total_tokens", 0) or 0)
    except (TypeError, ValueError):
        return None
    # Don't surface an all-zero token record — it just adds noise.
    if inp == 0 and outp == 0 and tot == 0:
        return None
    if tot == 0:
        tot = inp + outp
    return {"input": inp, "output": outp, "total": tot}

# ─────────────────────────────────────────────────────────────────
# Preset lookup —  per-user binding, round 2: strict
# ─────────────────────────────────────────────────────────────────
#
# LLM presets are strictly per-user — there is no system-shared tier
# any more. `_resolve_preset` / `_resolve_default_preset_id` accept
# an optional `user_id`; when set, the lookup is restricted to that
# user's own rows. When `user_id` is None, both helpers return
# nothing (the pre-binding "first match" behaviour is gone). The
# runtime threads the workflow owner's id through `CompileCtx`
# (set by `runtime_service.run_workflow`) so every agent node sees
# only the owner's presets.
#
# Visibility model: `user_id == X`. If the owner has no default AND
# no preset, `build_model` returns None and the agent handler
# surfaces the same "set a default preset" error as before.
def _resolve_preset(
    preset_id: str,
    user_id: str | None = None,
    *,
    db: Optional["Session"] = None,
) -> dict | None:
    """Look up a preset row and return its key/url as a dict (or None).

    Strict per-user visibility ( round 2): only the row
    owned by `user_id` matches. Other users' presets are invisible.
    When `user_id` is None (legacy / background path with no caller
    scope), the lookup returns None — the runtime guarantees
    `user_id` is set on every workflow execution.

    `db` : when supplied, the lookup runs against that
    session directly (no internal `session_scope()`). Mirrors
    `_resolve_default_preset_id`'s contract so unit tests can seed
    rows into their own in-memory engine and read them back without
    going through the production engine.
    """
    if not preset_id or not user_id:
        return None
    if db is None:
        with session_scope() as s:
            return _resolve_preset(preset_id, user_id, db=s)
    row = _find_preset(db, preset_id, user_id)
    if row is None:
        return None
    return {
        "name": row.name,
        "provider": row.provider,
        "model_id": row.model_id,
        "api_key": row.api_key,
        "base_url": row.base_url,
        # P3 : per-preset "thinking mode" toggle.
        # Surfaced here so `build_model` can honour the preset's
        # reasoning preference without a separate global flag.
        "thinking": bool(getattr(row, "thinking", False)),
    }

def _resolve_default_preset_id(
    db: Optional["Session"] = None,
    user_id: str | None = None,
) -> str | None:
    """Return the id of the user's chosen default preset, or None.

    Every Agent node inherits this when its own model config is empty
    (the common case — the PropertyPanel no longer offers a per-agent
    dropdown). Returns None when the caller has no default preset —
    the Agent handler surfaces a clear error and the frontend's
    PropertyPanel guard tells the user to set one.

     per-user binding (round 2, strict): when `user_id` is
    supplied, only that user's `is_default=true` rows match. No
    system-tier fallback — every user configures their own. When
    `user_id` is None the lookup returns None; the runtime always
    threads an id through, so this branch is the legacy / no-caller
    path only.

    `db` is optional; when None we open our own short-lived session via
    `session_scope()`. Tests pass the test-session explicitly so the
    resolution runs against the same in-memory DB as the rest of the
    test fixture.
    """
    if not user_id:
        return None
    if db is None:
        with session_scope() as s:
            return _resolve_default_preset_id(s, user_id=user_id)
    row = _find_default(db, user_id)
    return row.id if row is not None else None

def _find_preset(
    db: "Session",
    preset_id: str,
    user_id: str | None,
):
    """Single-row lookup honouring the strict per-user visibility.

    `user_id` is required (the runtime always supplies one). Any
    row whose `user_id` doesn't match is invisible — there's no
    system fallback.
    """
    if not user_id:
        return None
    return (
        db.query(LlmPreset)
        .filter(LlmPreset.id == preset_id, LlmPreset.user_id == user_id)
        .one_or_none()
    )

def _find_default(db: "Session", user_id: str | None):
    """Default lookup honouring the strict per-user scope.

    `user_id` is required. The first row owned by `user_id` with
    `is_default=true` wins; there's no system fallback in the
    strict-binding model.
    """
    if not user_id:
        return None
    return (
        db.query(LlmPreset)
        .filter(
            LlmPreset.user_id == user_id,
            LlmPreset.is_default == True,  # noqa: E712
        )
        .first()
    )

# ─────────────────────────────────────────────────────────────────
# Model construction
# ─────────────────────────────────────────────────────────────────
def build_model(
    model_cfg: dict,
    thinking: bool = False,
    *,
    user_id: str | None = None,
):
    """Instantiate an agno Model from a node's `config.model` dict.

    Resolution (platform-side, no environment fallback):
      1. If `presetId` is set, look up the preset (scoped to `user_id`
         when supplied — ); its
         provider/model_id/api_key/base_url/thinking become the
         effective values. The preset's `thinking` flag wins over any
         caller-supplied override — the row is the source of truth.
      2. Otherwise use the per-node `provider`/`modelId`/`apiKey`/`baseUrl`.
         `thinking` falls back to the caller-supplied value (default
         False) since there's no preset row to consult.

    `thinking` (P3, ): when True, pass the provider-specific
    reasoning kwargs so the model emits extended thinking content.
      - anthropic: `thinking={"type": "enabled", "budget_tokens": 1024}`
        (Claude refuses thinking on haiku-3.x — let agno raise).
      - openai: `reasoning_effort="medium"` (no-op for non-reasoning
        models; harmless for gpt-4o family).
      - google: `thinking_budget=1024` (Gemini 2.5+ only — older models
        ignore it).
      - ollama: no first-class param, left as a pass-through no-op.
    Default False — most preset rows store False too, so test runs
    stay fast.

    `user_id` : when the runtime threads the workflow
    owner's id, preset lookup is scoped to that user's presets + the
    shared system rows. This is what makes "alice's default preset"
    invisible to bob.

    Returns None if the model cannot be built (missing key, unknown
    provider, or empty everything). The caller (the agent handler)
    surfaces a clear ErrorEvent so the user knows to set a key.
    """
    if not model_cfg:
        return None

    # Layer 1: preset wins (and supplies the thinking flag).
    preset = _resolve_preset(model_cfg.get("presetId") or "", user_id=user_id)
    if preset:
        provider = preset["provider"]
        model_id = preset["model_id"]
        api_key = preset["api_key"] or ""
        base_url = preset["base_url"] or ""
        # The preset is the canonical source of the reasoning flag.
        # Whatever the caller passed in is overridden by the row.
        # `.get()` (not `[]`) so older test fixtures that don't include
        # `thinking` keep working — missing key defaults to False.
        thinking = bool(preset.get("thinking", False))
    else:
        provider = (model_cfg.get("provider") or "openai").lower()
        model_id = model_cfg.get("modelId") or ""
        api_key = model_cfg.get("apiKey") or ""
        base_url = model_cfg.get("baseUrl") or ""

    # No model id → caller's model config is incomplete. Platform doesn't
    # second-guess from `.env.llm` anymore — the user must set a preset.
    if not model_id:
        return None

    if provider == "openai":
        from agno.models.openai import OpenAIChat
        kw: dict = {"id": model_id}
        if api_key:
            kw["api_key"] = api_key
        if base_url:
            kw["base_url"] = base_url
        if thinking:
            kw["reasoning_effort"] = "medium"
        if not kw.get("api_key"):
            return None
        return OpenAIChat(**kw)

    if provider == "anthropic":
        from agno.models.anthropic import Claude
        kw = {"id": model_id}
        if api_key:
            kw["api_key"] = api_key
        if base_url:
            kw["client_params"] = {"base_url": base_url}
        if thinking:
            # agno's `_validate_thinking_support` raises ValueError if
            # the model is in NON_THINKING_MODELS (haiku-3.x). Let that
            # surface as a build failure — callers translate it into
            # a single ErrorEvent via _build_agent_for_node's wrapping.
            kw["thinking"] = {"type": "enabled", "budget_tokens": 1024}
        if not kw.get("api_key"):
            return None
        return Claude(**kw)

    if provider == "ollama":
        from agno.models.ollama import Ollama
        # Ollama has no first-class thinking param; pass-through no-op.
        return Ollama(id=model_id)

    if provider == "google":
        from agno.models.google import Gemini
        kw = {"id": model_id}
        if api_key:
            kw["api_key"] = api_key
        if thinking:
            # Gemini 2.5+ only — older models ignore thinking_budget.
            kw["thinking_budget"] = 1024
        return Gemini(**kw)

    return None

# ─────────────────────────────────────────────────────────────────
# Invocation
# ─────────────────────────────────────────────────────────────────
def call_model_sync(model, prompt: str, instructions: str | None = None) -> ModelResult:
    """Run a single-turn completion; return the final text + token counts.

    Uses agno's `Agent` to handle tool wiring (we have none in v1
    since tools are exec'd in the runtime), or `Model.response` directly
    if we just need raw text. The Agent path is more reliable across
    agno versions.
    """
    from agno.agent import Agent
    agent = Agent(
        model=model,
        instructions=instructions or "You are a helpful assistant.",
        markdown=False,
    )
    out = agent.run(prompt, stream=False)
    text: str
    # agno RunOutput has .content (str) on recent versions
    if hasattr(out, "content") and out.content:
        text = str(out.content)
    elif hasattr(out, "messages") and out.messages:
        last = out.messages[-1]
        text = str(getattr(last, "content", last))
    else:
        text = str(out)
    return ModelResult(text=text, tokens=_extract_tokens(out))

def stream_model(model, prompt: str, instructions: str | None = None) -> Iterator[str]:
    """Yield text chunks as the model streams them.

    NOTE: token counts are only available via the non-streaming
    `call_model_sync` path — agno's streaming API doesn't surface
    `RunOutput.metrics`. The agent handler picks `call_model_sync` when
    the trace panel needs tokens.
    """
    from agno.agent import Agent
    agent = Agent(
        model=model,
        instructions=instructions or "You are a helpful assistant.",
        markdown=False,
    )
    for chunk in agent.run(prompt, stream=True):
        if hasattr(chunk, "content") and chunk.content:
            yield str(chunk.content)
        elif hasattr(chunk, "delta") and chunk.delta:
            yield str(chunk.delta)