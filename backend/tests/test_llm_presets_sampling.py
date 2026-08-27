"""Per-preset sampling / length knobs (temperature / top_p / max_tokens).

Round-trip:
  * POST /api/v1/llm-presets with the three new fields → 201 + values
    come back via GET (camelCase).
  * PATCH to clear a field → the column flips back to NULL.
  * Out-of-range value → 422 (Pydantic `Field(ge/le)` rejects at the
    API, never reaches the DB).

Runtime injection:
  * `app.core.llm_runner.build_model` forwards the preset's knobs to
    the agno `Model` constructor (kwargs captured via a recording
    Mock), and omits them when the preset value is NULL.

These tests run against the same `client` + `seeded_default_preset`
fixtures as `test_llm_presets.py` — see `backend/tests/conftest.py`.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch

# Pre-import the agno provider modules so `patch("agno.models.X.Y",
# create=True)` can locate the submodule. `agno/models/__init__.py`
# doesn't auto-import every provider — without these lines, the
# tests below would fail with `module 'agno.models' has no attribute
# 'openai'` (etc.) even with `create=True`. `google` / `ollama` raise
# ImportError at import time when their respective SDKs aren't
# installed; gate each so this file still collects in lean dev envs.
try:
    import agno.models.openai  # noqa: F401
    OPENAI_AVAILABLE = True
except ImportError:  # pragma: no cover — openai ships with agno
    OPENAI_AVAILABLE = False

try:
    import agno.models.google  # noqa: F401
    GOOGLE_AVAILABLE = True
except ImportError:
    GOOGLE_AVAILABLE = False

try:
    import agno.models.ollama  # noqa: F401
    OLLAMA_AVAILABLE = True
except ImportError:
    OLLAMA_AVAILABLE = False

USER = {"X-User-Id": "sampling-tester@example.com"}


# ─────────────────────────────────────────────────────────────────
# Round-trip
# ─────────────────────────────────────────────────────────────────
def test_create_preset_with_sampling_returns_camelcase(client):
    """POST with temperature / topP / maxTokens → 201; the read
    returns the values in camelCase to match the frontend's
    `LlmPreset` interface (the same convention as `modelId` / `baseUrl`)."""
    r = client.post(
        "/api/v1/llm-presets",
        headers=USER,
        json={
            "name": "Sampling Claude",
            "provider": "anthropic",
            "modelId": "claude-sonnet-4-5",
            "apiKey": "sk-sampling",
            "temperature": 0.3,
            "topP": 0.9,
            "maxTokens": 512,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["temperature"] == 0.3
    assert body["topP"] == 0.9
    assert body["maxTokens"] == 512

    # Read back via GET and confirm camelCase + values stick.
    pid = body["id"]
    r = client.get(f"/api/v1/llm-presets/{pid}", headers=USER)
    assert r.status_code == 200, r.text
    fetched = r.json()
    assert fetched["temperature"] == 0.3
    assert fetched["topP"] == 0.9
    assert fetched["maxTokens"] == 512


def test_patch_preset_clears_sampling_field(client):
    """PATCH with `{"temperature": null}` flips the column back to NULL.

    The service uses `model_dump(exclude_unset=True)`, so an absent key
    leaves the column alone; a present-`None` value explicitly clears
    it. Both behaviours are part of the contract — the frontend's
    "user cleared the box" path relies on the latter."""
    # Create with all three set.
    r = client.post(
        "/api/v1/llm-presets",
        headers=USER,
        json={
            "name": "Will Clear",
            "provider": "openai",
            "modelId": "gpt-4o",
            "apiKey": "sk-clear",
            "temperature": 0.5,
            "topP": 0.8,
            "maxTokens": 1024,
        },
    )
    pid = r.json()["id"]
    assert r.json()["temperature"] == 0.5

    # Clear temperature only.
    r = client.patch(
        f"/api/v1/llm-presets/{pid}",
        headers=USER,
        json={"temperature": None},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["temperature"] is None
    # The other two remain untouched — `exclude_unset=True` honours the
    # "untouched = leave alone" contract.
    assert body["topP"] == 0.8
    assert body["maxTokens"] == 1024


def test_create_preset_rejects_out_of_range_temperature(client):
    """Pydantic validator catches it before the row is written."""
    r = client.post(
        "/api/v1/llm-presets",
        headers=USER,
        json={
            "name": "Bad temp",
            "provider": "openai",
            "modelId": "gpt-4o",
            "apiKey": "sk-bad",
            "temperature": 99,  # outside [0, 2]
        },
    )
    assert r.status_code == 422, r.text


def test_create_preset_rejects_out_of_range_top_p(client):
    r = client.post(
        "/api/v1/llm-presets",
        headers=USER,
        json={
            "name": "Bad topP",
            "provider": "openai",
            "modelId": "gpt-4o",
            "apiKey": "sk-bad",
            "topP": 1.5,  # outside [0, 1]
        },
    )
    assert r.status_code == 422, r.text


def test_create_preset_rejects_out_of_range_max_tokens(client):
    r = client.post(
        "/api/v1/llm-presets",
        headers=USER,
        json={
            "name": "Bad maxTokens",
            "provider": "openai",
            "modelId": "gpt-4o",
            "apiKey": "sk-bad",
            "maxTokens": 0,  # outside [1, 100000]
        },
    )
    assert r.status_code == 422, r.text


# ─────────────────────────────────────────────────────────────────
# Runtime injection — build_model forwards knobs to agno
# ─────────────────────────────────────────────────────────────────
class _Recorder:
    """Tiny stand-in for an agno Model that captures the kwargs
    passed to its constructor. Tests assert against `self.kwargs`."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _seed_preset(db, **overrides):
    """Insert a preset owned by `sampling-tester@example.com` and
    return its id. Keeps the tests below free of fixture boilerplate.

    `llm_presets.user_id` is a FK to `users.id`, so the User row has
    to exist first — `db.merge(User(...))` is the pattern `test_compile_agent_emitter.py`
    uses for the same reason. Without it the FK fails on Postgres."""
    from app.db.models import LlmPreset, User
    OWNER = "sampling-tester@example.com"
    db.merge(User(id=OWNER, email=OWNER))
    db.commit()
    pid = f"preset-{hash(tuple(sorted(overrides.items()))) & 0xFFFFFFFFFFFFFFFF:x}"
    row = LlmPreset(
        id=pid,
        name="Sampling",
        provider=overrides.pop("provider", "openai"),
        model_id=overrides.pop("model_id", "gpt-4o"),
        api_key=overrides.pop("api_key", "sk"),
        base_url=None,
        is_default=False,
        thinking=False,
        user_id=OWNER,
        **overrides,
    )
    db.add(row)
    db.commit()
    return pid


@pytest.mark.skipif(not OPENAI_AVAILABLE, reason="agno openai provider not importable")
def test_build_model_forwards_sampling_to_openai(seeded_default_preset, db):
    """Preset with all three knobs set → kwargs appear on the
    `OpenAIChat` constructor; NULL → kwargs are absent (omit-if-None)."""
    from app.core import llm_runner

    pid = _seed_preset(
        db,
        temperature=0.7,
        top_p=0.95,
        max_tokens=2048,
    )
    with patch("agno.models.openai.OpenAIChat", _Recorder, create=True):
        model = llm_runner.build_model({"presetId": pid}, user_id="sampling-tester@example.com")
    assert isinstance(model, _Recorder)
    assert model.kwargs.get("temperature") == 0.7
    assert model.kwargs.get("top_p") == 0.95
    assert model.kwargs.get("max_tokens") == 2048


@pytest.mark.skipif(not OPENAI_AVAILABLE, reason="agno openai provider not importable")
def test_build_model_omits_null_sampling(seeded_default_preset, db):
    """Preset with all NULL knobs → kwargs are absent on the Model."""
    from app.core import llm_runner

    pid = _seed_preset(db)  # no temperature / top_p / max_tokens
    with patch("agno.models.openai.OpenAIChat", _Recorder, create=True):
        model = llm_runner.build_model({"presetId": pid}, user_id="sampling-tester@example.com")
    assert isinstance(model, _Recorder)
    assert "temperature" not in model.kwargs
    assert "top_p" not in model.kwargs
    assert "max_tokens" not in model.kwargs


@pytest.mark.skipif(not GOOGLE_AVAILABLE, reason="agno google provider SDK not installed")
def test_build_model_renames_max_tokens_for_gemini(seeded_default_preset, db):
    """Gemini's parameter is `max_output_tokens` (not `max_tokens`) —
    `build_model` has to translate. Assert the rename + the
    sampling knobs land on the Gemini kwarg surface."""
    from app.core import llm_runner

    pid = _seed_preset(
        db,
        provider="google",
        model_id="gemini-2.5-flash",
        temperature=0.4,
        top_p=0.85,
        max_tokens=4096,
    )
    with patch("agno.models.google.Gemini", _Recorder, create=True):
        model = llm_runner.build_model({"presetId": pid}, user_id="sampling-tester@example.com")
    assert isinstance(model, _Recorder)
    assert model.kwargs.get("temperature") == 0.4
    assert model.kwargs.get("top_p") == 0.85
    assert model.kwargs.get("max_output_tokens") == 4096
    assert "max_tokens" not in model.kwargs


@pytest.mark.skipif(not OLLAMA_AVAILABLE, reason="agno ollama provider SDK not installed")
def test_build_model_uses_ollama_options_dict(seeded_default_preset, db):
    """Ollama takes `options={"temperature":..., "top_p":...,
    "num_predict":...}` — not flat kwargs. `build_model` builds the
    dict only when at least one knob is non-NULL."""
    from app.core import llm_runner

    pid = _seed_preset(
        db,
        provider="ollama",
        model_id="llama3.1",
        temperature=0.2,
        top_p=0.7,
        max_tokens=256,
    )
    with patch("agno.models.ollama.Ollama", _Recorder, create=True):
        model = llm_runner.build_model({"presetId": pid}, user_id="sampling-tester@example.com")
    assert isinstance(model, _Recorder)
    opts = model.kwargs.get("options")
    assert opts == {
        "temperature": 0.2,
        "top_p": 0.7,
        "num_predict": 256,
    }


@pytest.mark.skipif(not OPENAI_AVAILABLE, reason="agno openai provider not importable")
def test_build_model_inline_cfg_picks_up_sampling(seeded_default_preset):
    """The inline (no-preset) branch also honours the same keys —
    consistent with how `thinking` already flows from inline config."""
    from app.core import llm_runner

    with patch("agno.models.openai.OpenAIChat", _Recorder, create=True):
        model = llm_runner.build_model(
            {
                "provider": "openai",
                "modelId": "gpt-4o",
                "apiKey": "sk",
                "temperature": 0.5,
                "topP": 0.5,
                "maxTokens": 100,
            },
            user_id="sampling-tester@example.com",
        )
    assert isinstance(model, _Recorder)
    assert model.kwargs.get("temperature") == 0.5
    assert model.kwargs.get("top_p") == 0.5
    assert model.kwargs.get("max_tokens") == 100