"""Test-only helper for reading `.env.llm` credentials.

The platform no longer reads `.env.llm` at runtime — its LLM config
comes from the `LlmPreset` table. Tests that need real LLM credentials
(legacy env-var fallback, the smart-router fallback path, etc.) use the
helpers here so the boundary is clear: production code → presets, tests
→ `.env.llm`.

Two surfaces:

  * `load_llm_env()` — read `.env.llm` and return the parsed keys as a
    dict. No side effects on `os.environ`.

  * `live_llm_credentials` fixture — pytest fixture that yields the same
    dict. Use this in tests that want to mutate credentials inside a
    single test (the fixture takes care of copying the original values
    so the developer's shell env isn't disturbed).

Search order matches the previous `app.config._discover_llm_env_file`:
AGNOBUILDER_LLM_ENV_FILE override → backend/.env.llm → repo root .env.llm.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

def _discover_llm_env_file() -> str | None:
    override = os.environ.get("AGNOBUILDER_LLM_ENV_FILE")
    if override and Path(override).exists():
        return override
    backend_local = Path(__file__).resolve().parents[1] / ".env.llm"
    if backend_local.exists():
        return str(backend_local)
    repo_root = Path(__file__).resolve().parents[2] / ".env.llm"
    if repo_root.exists():
        return str(repo_root)
    return None

def _parse_env_file(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                out[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return out

def load_llm_env() -> dict[str, str]:
    """Return the parsed `.env.llm` keys as a dict (no os.environ writes)."""
    path = _discover_llm_env_file()
    return _parse_env_file(path) if path else {}

@pytest.fixture()
def live_llm_credentials():
    """Pytest fixture: parsed `.env.llm` keys, restored on teardown.

    Use this when a test needs to flip credentials in/out of the
    environment without leaking to other tests. The dict is the snapshot
    from the file at fixture setup time — mutate it freely.
    """
    snapshot = load_llm_env()
    yield snapshot
    # Teardown: restore the keys to whatever the shell env had. We only
    # touch the keys the test might have written; everything else in
    # `os.environ` is left untouched.
    for key in snapshot:
        os.environ.pop(key, None)
