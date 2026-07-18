"""Regression tests for `compile.emitters.agent` model resolution.

Background: a user who had configured a default LLM preset in
Settings was getting

    Agent '问候者' has no model — set a default LLM preset in Settings → LLM Models

when running the built-in `tpl-hello-world` template, even though the
default preset was configured. Root cause:

  * The agent emitter only consulted the user's default preset when
    `cfg.model` was entirely missing.
  * Built-in templates ship with a legacy `{provider, modelId}` stub
    on every agent node — `provider`/`modelId` are present, but
    `apiKey` is not (secrets live in the user's `LlmPreset` row).
  * So `cfg.model` was non-empty → the emitter skipped the
    default-preset branch and handed the stub straight to
    `build_model`, which returned `None` for anthropic with no key.
  * The "Agent has no model" CompileError fired.

The fix: when the inline config doesn't yield a buildable Model, fall
back to the owner's default preset before raising. An earlier
change had already removed the per-agent model dropdown from the
PropertyPanel, so an
inline config that fails to build a Model is always a legacy stub
the default preset should satisfy.

These tests pin the contract:

  * `tpl-hello-world`-style nodes (legacy `{provider, modelId}` stub)
    compile successfully when the caller has a default preset.
  * The default preset is scoped by `user_id` (so cross-user leakage
    is impossible).
  * When neither inline config nor default preset resolves, the
    canonical CompileError still fires (so the user sees a clear
    instruction).
  * A complete inline config (provider+modelId+apiKey) still wins
    over the default preset — the power-user override survives.

NOTE on the `seeded_default_preset` fixture:

  The fixture monkeypatches `_resolve_default_preset_id` and
  `_resolve_preset` to IGNORE the `user_id` argument (see the
  comment in `conftest.py` — "Tests that DO thread a specific
  `user_id` get the matching row" — that promise is not honoured by
  the actual lambda). Most tests don't care because they pass
  `user_id=None` and only one default preset exists. Tests in this
  file that specifically assert per-user scoping re-monkeypatch
  those helpers to honour `user_id` (via `_user_scoped_llm_runner`)
  so the assertion is meaningful.
"""
from __future__ import annotations

import uuid

import pytest

from app.core.compile import build_workflow
from app.core.compile.errors import CompileError
from app.db.models import LlmPreset

pytestmark = pytest.mark.usefixtures("seeded_default_preset")

# The `seeded_default_preset` fixture stamps its row under
# `user_id="tests"`. Tests that need a different owner stamp their
# own row, then re-monkeypatch the llm_runner helpers via
# `_user_scoped_llm_runner` to honour user_id.
FIXTURE_USER_ID = "tests"

HELLO_WORLD_AGENT_NODE = {
    "id": "ag",
    "type": "agent",
    "position": {"x": 0, "y": 0},
    "data": {
        "label": "问候者",
        "config": {
            "instructions": "你是一个友好的中文助手。",
            # Same stub the template ships with — provider+modelId
            # but NO apiKey. Pre-fix this made `build_model` return
            # None and the compile fail.
            "model": {
                "provider": "anthropic",
                "model_id": "claude-sonnet-4-5",
            },
        },
    },
}

def _user_scoped_llm_runner(monkeypatch, db) -> None:
    """Re-patch the llm_runner lookup helpers to honour `user_id`.

    The `seeded_default_preset` fixture's lambdas ignore `user_id`
    (see conftest.py), which makes per-user scoping impossible to
    assert through them. This re-monkeypatches the helpers to use
    the real `_find_default` / `_find_preset` lookups so cross-user
    guard tests can actually verify the strict-binding contract.

    The patch closes over the test's `db` session so the helper
    reads the test fixture's `LlmPreset` rows directly (production
    code paths call `_resolve_default_preset_id()` with no db arg
    and let it open its own `session_scope()` — we override that
    fallback to keep the test isolated).
    """
    import app.core.llm_runner as lr

    def _default(db_arg=None, user_id=None):
        if not user_id:
            return None
        row = lr._find_default(db_arg or db, user_id)
        return row.id if row is not None else None

    def _preset(preset_id, user_id=None, db_arg=None):
        if not preset_id or not user_id:
            return None
        row = lr._find_preset(db_arg or db, preset_id, user_id)
        if row is None:
            return None
        return {
            "name": row.name,
            "provider": row.provider,
            "model_id": row.model_id,
            "api_key": row.api_key,
            "base_url": row.base_url,
            "thinking": bool(getattr(row, "thinking", False)),
        }

    monkeypatch.setattr(lr, "_resolve_default_preset_id", _default)
    monkeypatch.setattr(lr, "_resolve_preset", _preset)

def _build_simple_workflow(user_id: str | None) -> object:
    """Compile a one-agent workflow and return the agno object."""
    return build_workflow(
        workflow_id="wf-test",
        name="test",
        db_nodes=[HELLO_WORLD_AGENT_NODE],
        db_edges=[],
        user_id=user_id,
    )

def test_template_stub_compiles_when_default_preset_is_set(
    client, db, seeded_default_preset
):
    """The user's bug: tpl-hello-world's `{provider, modelId}` stub
    must compile successfully when the caller has a default preset.

    Pre-fix: this raised `CompileError: Agent '问候者' has no model`.
    Post-fix: the agent emitter falls back to the default preset and
    the compile succeeds.
    """
    wf = _build_simple_workflow(user_id=FIXTURE_USER_ID)
    assert wf.steps, "expected at least one compiled step"
    step = wf.steps[0]
    agent_obj = getattr(step, "agent", None)
    assert agent_obj is not None, (
        f"expected the agent stub to compile to an Agent, got {step!r}"
    )
    # The agent's model should be a real instance built from the
    # default preset's row, NOT a None placeholder.
    assert getattr(agent_obj, "model", None) is not None, (
        "Agent built but its `model` is None — the default-preset "
        "fallback did not fire"
    )

def test_default_preset_owner_scope_is_enforced(
    client, db, seeded_default_preset, monkeypatch
):
    """Cross-user guard: a workflow whose ctx.user_id is a DIFFERENT
    user from the preset's owner must NOT pick up that preset — the
    compile falls through to the "no model" error.

    The `seeded_default_preset` fixture owns its row under
    `user_id="tests"`. Threading `user_id="alice"` through the
    compile must NOT see it (after re-patching the fixture's
    user-ignoring lambdas via `_user_scoped_llm_runner`).
    """
    _user_scoped_llm_runner(monkeypatch, db)

    with pytest.raises(CompileError) as ei:
        _build_simple_workflow(user_id="alice")
    assert "has no model" in str(ei.value), (
        f"expected the canonical 'no model' error for a foreign user, "
        f"got: {ei.value!r}"
    )

def test_complete_inline_config_wins_over_default_preset(
    client, db, seeded_default_preset
):
    """Power-user override survives: if a node ships a COMPLETE inline
    config (provider+modelId+apiKey), the emitter must use it — not
    silently swap in the default preset.

    We point the inline config at `openai` so any accidental
    fallback to the fixture's anthropic preset would be detectable
    (the Agent's model class wouldn't match).
    """
    node = {
        "id": "ag",
        "type": "agent",
        "position": {"x": 0, "y": 0},
        "data": {
            "label": "Inline",
            "config": {
                "instructions": "Use the inline model.",
                "model": {
                    "provider": "openai",
                    "modelId": "gpt-4o-mini",
                    "apiKey": "sk-inline-fixture",
                },
            },
        },
    }
    wf = build_workflow(
        workflow_id="wf-inline",
        name="inline",
        db_nodes=[node],
        db_edges=[],
        user_id=FIXTURE_USER_ID,
    )
    assert wf.steps
    agent_obj = wf.steps[0].agent
    model = agent_obj.model
    assert model is not None
    # Class name avoids importing `agno.models.openai` in the test
    # (and tolerates agno re-naming it in a future version).
    assert type(model).__name__ == "OpenAIChat", (
        f"expected the inline config to win (OpenAIChat), got "
        f"{type(model).__name__}; the default-preset fallback is "
        f"swallowing the user's inline override"
    )

def test_no_default_preset_and_no_inline_config_raises_clear_error(
    client, db, seeded_default_preset, monkeypatch
):
    """If the caller has no default preset AND the inline config is
    empty (or also missing), the canonical 'Agent has no model'
    error must still fire — the user needs a clear pointer to
    Settings.

    We simulate "no default preset" by re-patching
    `_resolve_default_preset_id` to return None for any user_id.
    """
    import app.core.llm_runner as lr

    monkeypatch.setattr(
        lr, "_resolve_default_preset_id",
        lambda db=None, user_id=None: None,
    )
    monkeypatch.setattr(
        lr, "_resolve_preset",
        lambda preset_id, user_id=None, db=None: None,
    )

    node = {
        "id": "ag",
        "type": "agent",
        "position": {"x": 0, "y": 0},
        "data": {
            "label": "Empty",
            "config": {
                "instructions": "Nothing.",
                # Empty model config — should never reach a real model.
                "model": {},
            },
        },
    }
    with pytest.raises(CompileError) as ei:
        build_workflow(
            workflow_id="wf-empty",
            name="empty",
            db_nodes=[node],
            db_edges=[],
            user_id=FIXTURE_USER_ID,
        )
    msg = str(ei.value)
    assert "Empty" in msg and "has no model" in msg, (
        f"expected canonical 'no model' error, got: {msg!r}"
    )

def test_user_specific_default_preset_wins_over_seed(
    client, db, seeded_default_preset, monkeypatch
):
    """A user-scoped default preset (different user_id from the
    fixture's "tests") must win over the fixture's row when the
    compile runs under that user's id.

    Pins that the user_id threading through `build_workflow` actually
    scopes the lookup. Pre-binding the runtime always passed `None`,
    so a per-user default preset could never resolve — this was a
    separate regression fix and the agent emitter's fallback must
    honour the same `user_id`.
    """
    _user_scoped_llm_runner(monkeypatch, db)

    # Stamp a separate default preset owned by "alice@example.com".
    alice_pid = f"preset-{uuid.uuid4().hex[:8]}"
    db.add(LlmPreset(
        id=alice_pid,
        name="Alice's OpenAI",
        provider="openai",
        model_id="gpt-4o-mini",
        api_key="sk-alice-fixture",
        base_url=None,
        is_default=True,
        thinking=False,
        user_id="alice@example.com",
    ))
    db.commit()

    node = {
        "id": "ag",
        "type": "agent",
        "position": {"x": 0, "y": 0},
        "data": {
            "label": "Scoped",
            "config": {
                "instructions": "Use alice's default.",
                "model": {
                    "provider": "anthropic",
                    "model_id": "claude-sonnet-4-5",
                    # No apiKey — the stub the template ships with.
                },
            },
        },
    }
    wf = build_workflow(
        workflow_id="wf-alice",
        name="alice",
        db_nodes=[node],
        db_edges=[],
        user_id="alice@example.com",
    )
    assert wf.steps
    agent_obj = wf.steps[0].agent
    # Alice's preset is `openai` — the compile must pick it up, NOT
    # the fixture's anthropic row.
    model = agent_obj.model
    assert model is not None
    assert type(model).__name__ == "OpenAIChat", (
        f"expected alice's openai preset to win, got "
        f"{type(model).__name__}"
    )