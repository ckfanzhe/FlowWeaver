"""Pytest fixtures: isolated Postgres DB + FastAPI test client.

Each test gets:
  - a fresh Postgres schema (function-scoped engine) so tests can't
    collide on shared rows. Schema-per-test is the cheapest Postgres
    isolation strategy that still lets us share one connection pool.
  - a FastAPI TestClient with `get_db` dependency overridden to that
    schema's engine.

Prereq: a running Postgres reachable at `AGNOBUILDER_TEST_DATABASE_URL`
(or the default below points at the docker-compose `postgres` service
on localhost:5432). The default user/db/password match
`docker-compose.yml::postgres` + `.env.example::POSTGRES_*` so a plain
`docker compose up postgres` followed by `pytest` works out of the box.
"""
from __future__ import annotations

import os

# Suppress the lifespan-level seed writes to the production DB —
# tests explicitly seed the test DB via the `seeded` fixture.
os.environ.setdefault("AGNOBUILDER_SKIP_SEED", "1")

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db import models  # noqa: F401  (register models)
from app.db.base import Base
from app.db.session import get_db
from app.main import app


# Default test URL — matches `docker-compose.yml::postgres` +
# `.env.example::POSTGRES_USER/PASSWORD/DB`. Override with
# `AGNOBUILDER_TEST_DATABASE_URL=postgresql://...` when CI's Postgres
# is reachable under a different host.
DEFAULT_TEST_DATABASE_URL = os.environ.get(
    "AGNOBUILDER_TEST_DATABASE_URL",
    "postgresql+psycopg://agnobuilder:agnobuilder@127.0.0.1:5432/agnobuilder",
)

@pytest.fixture()
def engine():
    """Per-test Postgres schema with drop+create on entry / exit.

    Schema-per-test gives full isolation without paying the cost of
    dropping / recreating every table. The connection URL is
    rewritten to point at a unique schema name (`test_<pid>_<uuid8>`)
    so parallel test workers + repeated runs in the same DB don't
    collide. `Base.metadata.create_all` / `drop_all` then operate on
    the schema-scoped metadata via the URL search path.
    """
    import uuid

    schema = f"test_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    base_url = DEFAULT_TEST_DATABASE_URL
    # Inject the schema via URL search_path so every connection
    # targets the test schema. `options=-c search_path=...` is the
    # portable Postgres incantation.
    url = base_url + ("&" if "?" in base_url else "?") + f"options=-c%20search_path%3D{schema}"
    eng = create_engine(url, echo=False)
    with eng.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    with eng.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    eng.dispose()


@pytest.fixture()
def db(engine):
    """Session bound to the test DB. Commits inside tests persist until teardown."""
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _load_dotenv_llm() -> dict[str, str]:
    """Read the repo-root `.env.llm` file into a dict.

    This file is a test-only contract: it carries a single Claude
    API key + base_url that integration tests use as the
    `LlmPreset.api_key` / `LlmPreset.base_url` so agent nodes can
    actually reach the LLM (instead of stubbing the model class).
    Production code MUST NOT read this file — the platform's
    LLM config is the user-managed `LlmPreset` table.
    """
    from pathlib import Path

    env_path = Path(__file__).resolve().parents[2] / ".env.llm"
    out: dict[str, str] = {}
    if not env_path.exists():
        return out
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


@pytest.fixture()
def seeded_default_preset(monkeypatch, db):
    """Seed a default Claude `LlmPreset` in the test DB and patch
    `_resolve_preset` / `_resolve_default_preset_id` so workflow tests
    see a usable default.

    Also stubs `Claude.invoke_stream` / `Claude.response` with a
    deterministic echo stub so existing test assertions (which pin
    the agent's text payload to the legacy `[label] echo: input`
    shape) keep working. The label is read off `model.name` (set by
    `Agent(name=...)` in `_build_agent_for_node`), so the legacy
    `[label] ` prefix is preserved end-to-end.

    Tests that want a real round-trip to the LLM should request the
    `real_llm_preset` fixture instead (see below).
    """
    import uuid

    from app.db.models import LlmPreset, User

    # LlmPreset.user_id is FK to users.id; insert the synthetic owner
    # FIRST so the FK passes. ON CONFLICT keeps repeated fixture
    # invocations within one test session idempotent.
    db.merge(User(
        id="tests",
        email="tests@local",
        # avatar/language/theme all null — fixture is the bare-minimum
        # user row; preferences live on the LlmPreset itself.
    ))
    db.commit()

    pid = f"preset-{uuid.uuid4().hex[:8]}"
    db.add(LlmPreset(
        id=pid,
        name="Default Claude",
        provider="anthropic",
        model_id="claude-sonnet-4-5",
        api_key="sk-test-fixture",
        base_url=None,
        is_default=True,
        thinking=False,  # default-off matches what tests rely on
        # Presets are strictly per-user; the test owner is a
        # synthetic id ("tests") so the fixture is visible under
        # the strict-binding filter. Tests that thread a different
        # `user_id` will see no presets, matching real behaviour.
        user_id="tests",
    ))
    db.commit()

    import app.core.llm_runner as lr

    real_default = lr._resolve_default_preset_id
    real_resolve = lr._resolve_preset

    def lookup(preset_id: str):
        if not preset_id:
            return None
        row = db.query(LlmPreset).filter_by(id=preset_id).one_or_none()
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

    # Agent nodes run via agno-native `Step(agent=Agent(...))` —
    # there is no longer a `_agent_handler` wrapper to
    # re-register. We let `_build_agent_for_node` do its real
    # thing (it calls `llm_runner.build_model` which resolves
    # the default preset and builds a real `Claude` model), then
    # stub `Claude.invoke` / `Claude.invoke_stream` to return a canned
    # `ModelResponse` with the legacy echo content.
    #
    # IMPORTANT: we yield a *real* `agno.models.response.ModelResponse`
    # instance, not a hand-rolled duck-type. The previous fake `_EchoResponse`
    # class was missing fields that agno's `Agent.run()` loop inspects
    # (e.g. `created_at` / `input_tokens` / `stop_reason`-derived
    # event markers), so the agent loop silently dropped the stubbed
    # chunk and EventAdapter never saw a `RunContentEvent` to translate
    # into a `TextEvent`. Using the real dataclass keeps all fields
    # populated (defaults from `@dataclass(field(default_factory=...))`),
    # so agno's stream protocol works end-to-end and the canned
    # `[label] echo: input` text reaches the SSE stream exactly like
    # the legacy `_agent_handler` produced it.
    #
    # The stub now hooks `invoke` / `ainvoke` / `invoke_stream` /
    # `ainvoke_stream` — agno 2.8.7's call path goes through
    # `_process_model_response(...)` → `self.invoke(...)`, NOT
    # `self.response(...)`. Patching `Claude.response` is a no-op.
    #
    # The label is read off `agent.name` (set by `Agent(name=...)` in
    # the compile pipeline). For the agent-internal `Agent` instance
    # (the one `Wf.run()` constructs) we propagate `agent.name` onto
    # the model via `model.name = agent.name` BEFORE invoking agno —
    # the agent's `name` is what tests assert on, and agno's model
    # doesn't inherit it automatically.
    import agno.models.anthropic as _anthropic_mod
    from agno.models.response import ModelResponse

    def _build_echo_response(model_self, messages) -> ModelResponse:
        label = (
            getattr(model_self, "name", None)
            or "agent"
        )
        user_text = ""
        for m in reversed(messages):
            if getattr(m, "role", None) == "user":
                user_text = getattr(m, "content", "") or ""
                break
        return ModelResponse(
            role="assistant",
            content=f"[{label}] echo: {user_text}",
        )

    def _echo_invoke(model_self, messages, **kwargs):
        return _build_echo_response(model_self, messages)

    def _echo_stream(model_self, messages, **kwargs):
        # Yield a single full echo response then stop. agno's agent
        # loop terminates when tool_calls is empty and event is "".
        yield _build_echo_response(model_self, messages)

    monkeypatch.setattr(_anthropic_mod.Claude, "invoke", _echo_invoke)
    monkeypatch.setattr(_anthropic_mod.Claude, "invoke_stream", _echo_stream)
    monkeypatch.setattr(
        _anthropic_mod.Claude, "ainvoke",
        lambda self, *a, **kw: _echo_invoke(self, *a, **kw),
    )
    monkeypatch.setattr(
        _anthropic_mod.Claude, "ainvoke_stream",
        lambda self, *a, **kw: _echo_stream(self, *a, **kw),
    )

    # Hook Agent.run so we can stamp the agent's name onto its model.
    # The agent constructor sets `self.name = name` but agno doesn't
    # propagate that onto the model — so `_echo_invoke` would see
    # `model.name=None` and fall back to "agent". Patching here keeps
    # every test's compiled agent faithful to its label.
    import agno.agent as _agent_mod
    _real_agent_init = _agent_mod.Agent.__init__

    def _patched_agent_init(self, *args, **kwargs):
        _real_agent_init(self, *args, **kwargs)
        # agno 2.8.7 sets `self.name` from kwargs; mirror it onto the
        # model so legacy `[label] echo: ...` assertions still match.
        # Model.name defaults to the class name (e.g. "Claude",
        # "OpenAIChat") — we overwrite unconditionally so the stub
        # sees the agent's user-facing label.
        agent_name = getattr(self, "name", None)
        model = getattr(self, "model", None)
        if agent_name and model is not None:
            try:
                model.name = agent_name
            except Exception:  # noqa: BLE001
                pass

    monkeypatch.setattr(_agent_mod.Agent, "__init__", _patched_agent_init)

    monkeypatch.setattr(
        lr, "_resolve_default_preset_id",
        # Tests that don't thread `user_id` (the common case — most
        # workflow tests pass None because they exercise the runtime,
        # not the per-user auth path) still need a default preset to
        # resolve. The fixture row above is the answer for all of
        # them. Tests that DO thread a specific `user_id` get the
        # matching row (or None when there isn't one) — same contract
        # the production code path enforces.
        lambda db=None, user_id=None: pid,
    )
    monkeypatch.setattr(
        lr, "_resolve_preset",
        lambda preset_id, user_id=None, db=None: lookup(preset_id),
    )

    # Mirror the patch onto every module that imports the symbol by
    # name. Python's `from x import y` creates a SEPARATE binding in
    # the importing module's namespace — patching `lr._resolve_*`
    # alone is not enough if another module's code looks the name up
    # locally (e.g. `chat_builder_service._build_llm_model` calling
    # `_resolve_default_preset_id(db=db, ...)`). Mirror onto each
    # importer so all code paths see the fixture stub.
    for _modname in (
        "app.services.chat_builder_service",
    ):
        try:
            _mod = __import__(_modname, fromlist=["_resolve_default_preset_id"])
        except Exception:  # noqa: BLE001
            continue
        if hasattr(_mod, "_resolve_default_preset_id"):
            monkeypatch.setattr(
                _mod, "_resolve_default_preset_id",
                lambda db=None, user_id=None: pid,
            )
        if hasattr(_mod, "_resolve_preset"):
            monkeypatch.setattr(
                _mod, "_resolve_preset",
                lambda preset_id, user_id=None, db=None: lookup(preset_id),
            )

    return pid


@pytest.fixture()
def real_llm_preset(monkeypatch, db):
    """Seed a default Claude `LlmPreset` using credentials from the
    repo-root `.env.llm` file — and DON'T patch the model class. The
    agent actually calls the LLM, exercising the full agno-native
    `Step(agent=Agent(...))` round-trip:

      - `Agent.run()` produces a real `RunOutput`
      - agno emits `RunContentEvent`s (one per chunk during streaming)
      - EventAdapter translates them to `TextEvent`s on our SSE stream
      - the workflow finishes with a real `CompletedEvent`

    This fixture exists alongside `seeded_default_preset` because the
    majority of the existing test suite pins agent text to a
    deterministic `[label] echo: input` shape (a legacy contract
    that pinned agent text across many older tests). Forcing the
    real LLM onto those tests would make their assertions
    non-deterministic. Only tests that explicitly need to
    verify the agno-native translation pipeline should request this
    fixture — typically `tests/test_agno_native_steps.py`.

    Skips silently if `.env.llm` is absent (CI without secrets) so
    those tests can still be collected and reported; the test author
    can decide whether to mark them `@pytest.mark.integration` or
    guard the body on `pytest.importorskip(...)`.
    """
    import uuid

    from app.db.models import LlmPreset, User

    creds = _load_dotenv_llm()
    # Provider-agnostic (2026-08-14): the local vLLM config in
    # `.env.llm` uses OPENAI_* keys; the legacy anthropic config uses
    # ANTHROPIC_*. Whichever block is active (i.e. has its key set)
    # wins — OPENAI_* takes priority because it's the new default.
    if creds.get("OPENAI_API_KEY"):
        provider = "openai"
        api_key = creds.get("OPENAI_API_KEY")
        base_url = creds.get("OPENAI_BASE_URL")
        default_model = "Qwen3-8B"
    elif creds.get("ANTHROPIC_API_KEY"):
        provider = "anthropic"
        api_key = creds.get("ANTHROPIC_API_KEY")
        base_url = creds.get("ANTHROPIC_BASE_URL")
        default_model = "claude-sonnet-4-5"
    else:
        pytest.skip(".env.llm is missing — real_llm_preset needs an API key")

    pid = f"preset-real-{uuid.uuid4().hex[:8]}"
    # `llm_presets.user_id` is FK to `users.id`. Insert the
    # synthetic owner FIRST so the FK passes (mirrors the same
    # pattern in `seeded_default_preset`).
    db.merge(User(id="tests", email="tests@local"))
    db.commit()
    db.add(LlmPreset(
        id=pid,
        name=f"Default {provider} (.env.llm)",
        provider=provider,
        model_id=creds.get("BASE_MODEL") or default_model,
        api_key=api_key,
        base_url=base_url,
        is_default=True,
        thinking=False,
        # Presets are strictly per-user; see
        # `seeded_default_preset`. The synthetic "tests" owner
        # makes the row visible to the strict-binding helper.
        # Workflow tests that don't thread a `user_id` get this
        # row via the stubbed helper below.
        user_id="tests",
    ))
    db.commit()

    import app.core.llm_runner as lr

    real_default = lr._resolve_default_preset_id

    def lookup(preset_id: str):
        if not preset_id:
            return None
        row = db.query(LlmPreset).filter_by(id=preset_id).one_or_none()
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

    monkeypatch.setattr(
        lr, "_resolve_default_preset_id",
        # See `seeded_default_preset`: tests don't always thread
        # `user_id`, so the fixture row is the canonical answer.
        lambda db=None, user_id=None: pid,
    )
    monkeypatch.setattr(
        lr, "_resolve_preset",
        lambda preset_id, user_id=None, db=None: lookup(preset_id),
    )

    return pid


@pytest.fixture()
def client(engine) -> Generator[TestClient, None, None]:
    """FastAPI test client with `get_db` pointing at the test engine."""

    def _override_get_db():
        SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _bind_session_store_engine(engine, monkeypatch):
    """Every test (including the `test_session_store.py` ones that
    construct `SessionStore()` directly) uses the per-test
    in-memory SQLite engine, not the production
    `app.db.session.engine`. Without this autouse fixture, a test
    that does `store.create(...)` would write to the production
    DB.

    `SessionStore._engine` is a class attribute (late-bound to
    `app.db.session.engine` on first `__init__`); we monkeypatch
    it BEFORE the test body runs, so every `SessionStore()` /
    `session_store()` invocation inside the test uses the test
    engine. Restoration on teardown returns the class attribute
    to its previous value (None in fresh processes).

    Also rewires `app.db.session.engine` and `app.db.session.SessionLocal`
    to the per-test engine. Without this, `TestClient(app)` triggers
    the FastAPI lifespan which calls `init_db()` on the production
    engine — that engine points at `settings.database_url` which
    defaults to the compose-network hostname `postgres:5432` (NOT
    `127.0.0.1:5432`). On a developer laptop with docker-compose
    running, `postgres` doesn't resolve from the host process; the
    lifespan raises `failed to resolve host 'postgres'` and every
    subsequent test that touches the FastAPI app errors out with
    `sqlalchemy.exc.OperationalError`.

    Pinned so a future "let's also patch <X>" addition doesn't
    regress this — the test isolation contract is: every DB
    operation in a test MUST go through the per-test engine, no
    exceptions.
    """
    from app.runtime.session import SessionStore

    import app.db.session as _db_session

    monkeypatch.setattr(SessionStore, "_engine", engine)
    # Re-bind the module-level singletons so `app.db.session.engine`,
    # `app.db.session.SessionLocal`, and any code path that reads
    # `app.db.session.engine` (e.g. `init_db()` called from the
    # FastAPI lifespan) all use the per-test engine.
    monkeypatch.setattr(_db_session, "engine", engine)
    # `SessionLocal` is a sessionmaker bound to the original engine
    # — we need a fresh sessionmaker pointed at the test engine so
    # the FastAPI `get_db` dependency returns sessions that talk to
    # the right schema.
    from sqlalchemy.orm import sessionmaker

    _test_session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(_db_session, "SessionLocal", _test_session_local)