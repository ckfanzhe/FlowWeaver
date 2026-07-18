"""Bridge between agno's session/DB primitives and our FastAPI layer.

This is a thin shim around `agno.db.base.BaseDb` and `agno.session.WorkflowSession`
that:

  * Picks an appropriate `BaseDb` implementation based on the
    `AGNO_SESSION_BACKEND` env var (`memory` / `sqlite` / `redis`).
  * Lazily constructs a process-wide singleton so tests get a clean DB per
    test run (we monkeypatch the env var before the first call).
  * Provides `get_or_create_session` / `save_session` helpers that hide
    agno's `deserialize=True` toggle from callers.

We deliberately DO NOT replace `app.runtime.session.RuntimeSession` here —
that class is still used by the legacy executor shim during the migration.
New code should import from `app.core.session_bridge` instead.

Cluster mode (Redis) is documented in `docs/cluster-deployment.md`. The
Redis backend is NOT wired up this round — only the abstraction lives here.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from agno.db.base import BaseDb, SessionType

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# DB factory — single source of truth for "which BaseDb am I using"
# ─────────────────────────────────────────────────────────────────
def build_db() -> BaseDb:
    """Return a `BaseDb` matching `AGNO_SESSION_BACKEND`.

    Defaults to `InMemoryDb` (single-process dev / tests). `sqlite`
    uses `SqliteDb` with `db_url=sqlite:///<AGNO_SQLITE_PATH>` (default
    `./agno_sessions.db`). `redis` raises NotImplementedError because
    Phase D of the refactor plan defers the actual Redis wiring; see
    `docs/cluster-deployment.md` for the migration path.
    """
    backend = (os.environ.get("AGNO_SESSION_BACKEND") or "memory").lower()
    if backend == "memory":
        from agno.db.in_memory import InMemoryDb
        return InMemoryDb()
    if backend == "sqlite":
        from agno.db.sqlite import SqliteDb
        db_path = os.environ.get("AGNO_SQLITE_PATH") or "agno_sessions.db"
        return SqliteDb(db_url=f"sqlite:///{db_path}")
    if backend == "redis":
        raise NotImplementedError(
            "Redis backend is documented in docs/cluster-deployment.md but "
            "not wired up this round. Use AGNO_SESSION_BACKEND=sqlite for "
            "single-node persistence."
        )
    raise ValueError(f"unknown AGNO_SESSION_BACKEND: {backend!r}")

# ─────────────────────────────────────────────────────────────────
# Process-wide singleton — one BaseDb per worker process
# ─────────────────────────────────────────────────────────────────
_db: Optional[BaseDb] = None
_db_lock = threading.Lock()

def get_db() -> BaseDb:
    """Return the process-wide BaseDb (lazy singleton).

    Tests can call `reset_db()` to wipe and re-create between cases.
    """
    global _db
    if _db is None:
        with _db_lock:
            if _db is None:
                _db = build_db()
    return _db

def reset_db() -> None:
    """Drop the cached singleton so the next `get_db()` call re-reads the
    env var. Used by tests."""
    global _db
    with _db_lock:
        if _db is not None:
            try:
                _db.close()
            except Exception:  # noqa: BLE001
                pass
        _db = None

# ─────────────────────────────────────────────────────────────────
# WorkflowSession helpers — thin wrapper around agno's CRUD
# ─────────────────────────────────────────────────────────────────
def get_or_create_session(
    session_id: str,
    workflow_id: str,
    user_id: Optional[str] = None,
) -> "agno.session.WorkflowSession":  # noqa: F821
    """Load a `WorkflowSession` from the DB or return a fresh empty one.

    Returns a freshly-constructed `WorkflowSession` (not yet persisted)
    when nothing with this id exists yet — callers must call
    `save_session()` after the run completes to durably record it.
    """
    from agno.session.workflow import WorkflowSession

    db = get_db()
    existing = db.get_session(
        session_id=session_id,
        session_type=SessionType.WORKFLOW,
        user_id=user_id,
        deserialize=True,
    )
    if existing is not None:
        return existing

    return WorkflowSession(
        session_id=session_id,
        workflow_id=workflow_id,
        user_id=user_id,
        runs=[],
    )

def save_session(session: "agno.session.WorkflowSession") -> None:  # noqa: F821
    """Persist a `WorkflowSession` (and any new runs it carries) to the DB.

    agno's `BaseDb.upsert_session` handles inserts and updates transparently.
    """
    db = get_db()
    db.upsert_session(session)

def delete_session(session_id: str, user_id: Optional[str] = None) -> bool:
    """Remove a session from the DB. Returns True if a row was deleted."""
    db = get_db()
    return db.delete_session(session_id=session_id, user_id=user_id)