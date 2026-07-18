"""Two-tier runtime session store: in-memory hot cache + SQLite cold store.

/ session  — the slim `RuntimeSession` survives
process restart. The store shape is:

  - In-memory hot cache (`_cache: dict[sid, RuntimeSession]`) for
    fast in-process reads; same lifetime as the process.
  - SQLite cold store (`runtime_sessions` table via
    `RuntimeSessionRow` in `app.db.models`) for durability across
    restarts.

Mutations on the cached `RuntimeSession` stay free of DB I/O during
a leg. `RuntimeSession.flush()` mirrors the current state to the DB
at well-defined checkpoints (end of `_finalise_leg`, end of harness
finalisation, etc.). The cache is read-through: cache miss → DB
load → cache populate.

The `wf` field is transient (compiled `agno.Workflow`, not
serializable). After a restart the slim session comes back with
`wf=None`; the narrow recompile-on-load paths in
`runtime_service.continue_workflow` and `runtime_service.cancel_session`
rebuild it via the existing `build_workflow(...)` call.

/  multi-user isolation (round 3) is preserved:
`user_id` is a column on `RuntimeSessionRow`, and `get_for_user`
enforces the ownership check via `_owns(...)`.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.core.events import RuntimeEvent

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from agno.workflow import Workflow

@dataclass
class RuntimeSession:
    id: str
    workflow_id: str
    input: str
    # / : owner of this session. `None` only for
    # legacy / test rows; the HTTP layer rejects `continue` /
    # `get` for those when a real caller is supplied.
    user_id: str | None = None
    # / session: transient (not persisted). Rebuilt by
    # the recompile-on-load paths after a restart.
    wf: "Workflow | None" = None
    # Last `run_id` produced by `Wf.run(...)`. Replaces the
    # module-level `_RUN_ID_BY_SESSION` dict in `core.event_adapter`.
    run_id: str | None = None
    # `step_requirements` carried over from the last pause event.
    pending_requirements: list = field(default_factory=list)
    # step_id → original node type. The EventAdapter reads this
    # for `NodeStartEvent.nodeType` (trace panel icon picker).
    node_types: dict[str, str] = field(default_factory=dict)
    status: str = "running"  # running | waiting_confirmation | completed | error
    output: str | None = None
    history: list[dict] = field(default_factory=list)
    # Monotonic origin captured at session creation.
    started_at: float = 0.0
    # / session : last-touch timestamp (monotonic).
    # Updated by `SessionStore.get` / `get_for_user` so the cleanup
    # cron drops sessions idle past `SESSION_TTL_SECONDS`.
    last_seen_at: float = 0.0
    # / session : `workflow.updated_at` snapshot at
    # session creation. `_run_leg` compares against the live
    # workflow's `updated_at` to detect mid-conversation changes.
    workflow_updated_at: datetime | None = None

    def append_event(self, event: RuntimeEvent) -> None:
        self.history.append(event.model_dump())

    def set_last_run_id(self, run_id: str | None) -> None:
        self.run_id = run_id

    def get_last_run_id(self) -> str | None:
        return self.run_id

    def set_last_step_requirements(self, reqs: list | None) -> None:
        self.pending_requirements = list(reqs or [])

    def get_last_step_requirements(self) -> list:
        return list(self.pending_requirements or [])

    def flush(self) -> None:
        """/ session: mirror current state to SQLite.

        Called by the runtime service at well-defined checkpoints:
        end of `_finalise_leg`, end of harness finalisation. The
        `wf` reference is NOT persisted (compiled `agno.Workflow`
        is not JSON-serializable; the recompile-on-load paths in
        `runtime_service.continue_workflow` / `cancel_session`
        rebuild it).

        Idempotent — the underlying `SessionStore._write_row`
        does a PK lookup + UPDATE, so calling `flush()` twice
        with no intermediate mutation is a no-op.
        """
        from app.runtime.session import session_store

        session_store()._write_row(self)

class SessionStore:
    """Process-wide singleton. Two-tier: cache + SQLite.

    Cache (`_cache`) is the hot read path; SQLite is the durable
    cold store. Mutations go to the cache freely; `flush()` mirrors
    to SQLite. Cache misses read-through from SQLite.

    The `_engine` class attribute is late-bound so tests can swap
    it: production resolves to `app.db.session.engine` on first
    `__init__`; tests inject the per-test engine via the autouse
    fixture in `conftest.py`.

    Storage shape
    -------------
    Sessions live in `_cache: dict[sid, RuntimeSession]` for O(1)
    in-process lookup by sid. The DB index is the source of truth
    for cross-restart durability; `_by_user` / per-user filtering
    are SQL queries (`WHERE user_id = ?`), not in-memory indexes.
    `RLock` guards the cache mutations only; the DB has its own
    concurrency model (SQLite WAL).
    """

    _instance: "SessionStore | None" = None
    _singleton_lock = threading.Lock()
    # RLock because some callers hold it across multiple store
    # calls — e.g. `delete` then re-read via `get_for_user` in a
    # unit test.
    _lock: threading.RLock
    # Late-bound engine. Production resolves lazily on first
    # __init__; tests set this explicitly via the autouse fixture.
    _engine = None

    def __init__(self):
        self._cache: dict[str, RuntimeSession] = {}
        self._lock = threading.RLock()
        if SessionStore._engine is None:
            from app.db.session import engine

            SessionStore._engine = engine

    # ─────────────────────────────────────────────────────────────
    # Singleton plumbing.
    # ─────────────────────────────────────────────────────────────
    @classmethod
    def instance(cls) -> "SessionStore":
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _scope(self):
        """Return a session context manager bound to
        `SessionStore._engine` (NOT `app.db.session.engine`
        directly — the conftest autouse fixture swaps this class
        attribute to the per-test in-memory SQLite so unit tests
        don't pollute the production DB).

        `expire_on_commit=False` keeps loaded row attributes
        accessible after the `with` block exits — `_row_to_session`
        is called outside the block without triggering
        `DetachedInstanceError`. The wrapper commits on success,
        rolls back on exception, always closes — mirroring
        `app.db.session.session_scope` (which we can't reuse
        because it binds to the production engine).

        The sessionmaker is NOT cached on the instance: tests
        swap `SessionStore._engine` per-test via monkeypatch,
        so a cached sessionmaker would bind to a stale engine
        after the swap. Reading `_engine` fresh per call is
        cheap (~10μs) and keeps every call in lockstep with the
        currently-active engine.

        Usage: `with self._scope() as db: ...; row = ...`;
        safe to keep using `row` after the block."""
        from sqlalchemy.orm import sessionmaker

        db = sessionmaker(
            bind=SessionStore._engine,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )()

        class _Scope:
            def __enter__(self_inner):
                return db

            def __exit__(self_inner, exc_type, exc, tb):
                try:
                    if exc_type is None:
                        db.commit()
                    else:
                        db.rollback()
                finally:
                    db.close()
                return False

        return _Scope()

    # ─────────────────────────────────────────────────────────────
    # Create — INSERT into SQLite + populate the cache.
    # ─────────────────────────────────────────────────────────────
    def create(
        self,
        workflow_id: str,
        input: str,
        *,
        user_id: str | None,
        session_id: str | None = None,
        workflow_updated_at: "datetime | None" = None,
    ) -> RuntimeSession:
        """Create a session owned by `user_id`. Inserts the row in
        SQLite first (so any subsequent read in this process sees
        it), then populates the cache. Returns the in-memory
        `RuntimeSession` for the caller to mutate.

        `workflow_updated_at` (/ session): snapshot of the
        workflow row's `updated_at` at session creation. The
        `_run_leg` reuse branch compares this against the live
        workflow's `updated_at` to detect mid-conversation edits.
        """
        from app.db.models import RuntimeSessionRow

        sid = session_id or f"run-{uuid.uuid4().hex[:8]}"
        now = time.monotonic()
        sess = RuntimeSession(
            id=sid,
            workflow_id=workflow_id,
            input=input,
            user_id=user_id,
            started_at=now,
            last_seen_at=now,
            workflow_updated_at=workflow_updated_at,
        )
        # INSERT first so the row exists before any cache lookup.
        # Default fields (`status="running"`, empty JSON lists) are
        # set on the model definition; no need to pass them here.
        with self._scope() as db:
            db.add(
                RuntimeSessionRow(
                    id=sid,
                    workflow_id=workflow_id,
                    user_id=user_id,
                    input=input,
                    workflow_updated_at=workflow_updated_at,
                    started_at=now,
                    last_seen_at=now,
                )
            )
        with self._lock:
            self._cache[sid] = sess
        return sess

    # ─────────────────────────────────────────────────────────────
    # Read-through lookup. `get` is hot (per-event from the
    # EventAdapter); `get_for_user` is cold (per-request from
    # HTTP handlers). They have different `last_seen_at`
    # persistence policies — see D in the plan.
    # ─────────────────────────────────────────────────────────────
    def get(self, session_id: str) -> RuntimeSession | None:
        """Unscoped lookup. Cache hit → cache only (no DB write);
        cache miss → DB read + cache populate + DB write of the
        refreshed `last_seen_at`. The EventAdapter's per-event
        reads stay cheap because the DB is not touched on hit."""
        from app.db.models import RuntimeSessionRow

        with self._lock:
            sess = self._cache.get(session_id)
            if sess is not None:
                sess.last_seen_at = time.monotonic()
                return sess
        # Cache miss — read-through from SQLite.
        with self._scope() as db:
            row = db.get(RuntimeSessionRow, session_id)
        if row is None:
            return None
        sess = self._row_to_session(row)
        sess.last_seen_at = time.monotonic()
        self._write_last_seen(sess)
        with self._lock:
            self._cache[session_id] = sess
        return sess

    def get_for_user(
        self,
        session_id: str,
        user_id: str | None,
    ) -> RuntimeSession | None:
        """Owner-scoped lookup. Cache hit → DB write of refreshed
        `last_seen_at`; cache miss → DB read + ownership check +
        cache populate + DB write. The DB write on cache hit is
        the user-facing TTL extension signal — `cleanup_idle`
        relies on it to know "is the user actively using this?".

        Returns the session iff:
          * it exists, AND
          * its `user_id` matches the supplied caller, OR
          * the session is anonymous AND the caller is also anonymous
            (back-compat for templates / smoke tests).
        Returns None otherwise — the HTTP layer turns that into
        403 (when the session exists but is not yours) or 404
        (when no such sid exists at all).
        """
        from app.db.models import RuntimeSessionRow

        with self._lock:
            sess = self._cache.get(session_id)
            if sess is not None and self._owns(sess.user_id, user_id):
                sess.last_seen_at = time.monotonic()
                self._write_last_seen(sess)
                return sess
        # Cache miss — read-through + ownership check.
        with self._scope() as db:
            row = db.get(RuntimeSessionRow, session_id)
        if row is None:
            return None
        if not self._owns(row.user_id, user_id):
            return None
        sess = self._row_to_session(row)
        sess.last_seen_at = time.monotonic()
        self._write_last_seen(sess)
        with self._lock:
            self._cache[session_id] = sess
        return sess

    @staticmethod
    def _owns(row_user_id: str | None, caller_user_id: str | None) -> bool:
        """Ownership check. Anonymous↔anonymous is allowed
        (back-compat); otherwise `row_user_id == caller_user_id`."""
        if row_user_id is None and caller_user_id is None:
            return True
        if row_user_id is not None and row_user_id == caller_user_id:
            return True
        return False

    # ─────────────────────────────────────────────────────────────
    # Delete — evict from cache + remove the row.
    # ─────────────────────────────────────────────────────────────
    def delete(self, session_id: str) -> None:
        """Remove the session from cache and SQLite. Idempotent —
        missing rows are silently ignored."""
        from app.db.models import RuntimeSessionRow

        with self._lock:
            self._cache.pop(session_id, None)
        with self._scope() as db:
            row = db.get(RuntimeSessionRow, session_id)
            if row is not None:
                db.delete(row)

    # ─────────────────────────────────────────────────────────────
    # Cleanup — DB sweep with `status != 'running'` carve-out.
    # ─────────────────────────────────────────────────────────────
    def cleanup_idle(self, ttl_seconds: float) -> int:
        """/ session: drop sessions idle longer than `ttl_seconds`.

        Sweeps SQLite (not the cache) so cross-restart idle rows
        are reaped too. Carve-out: `status == 'running'` rows are
        NEVER dropped — an in-flight agno loop must not be GC'd
        mid-run (the EventAdapter needs the slim session's
        `run_id` to translate events). `waiting_confirmation` and
        `completed` are treated as idle-eligible.

        Returns the count of dropped rows. The background task in
        `app.main.lifespan` calls this every 60s with
        `ttl_seconds=1800` (30 min).
        """
        from app.db.models import RuntimeSessionRow

        if ttl_seconds <= 0:
            return 0
        cutoff = time.monotonic() - ttl_seconds
        with self._scope() as db:
            stale = (
                db.query(RuntimeSessionRow)
                .filter(
                    RuntimeSessionRow.status != "running",
                    RuntimeSessionRow.last_seen_at < cutoff,
                )
                .all()
            )
            dropped_ids = [r.id for r in stale]
            for row in stale:
                db.delete(row)
        with self._lock:
            for sid in dropped_ids:
                self._cache.pop(sid, None)
        if dropped_ids:
            log.info(
                "session cleanup: dropped %d idle session(s) past ttl=%.1fs",
                len(dropped_ids),
                ttl_seconds,
            )
        return len(dropped_ids)

    # ─────────────────────────────────────────────────────────────
    # List — SQL query over `runtime_sessions`.
    # ─────────────────────────────────────────────────────────────
    def list_for_user(self, user_id: str) -> list[RuntimeSession]:
        """Snapshot a user's live sessions. Read-only — callers
        must not mutate the returned objects without re-taking
        the lock."""
        from app.db.models import RuntimeSessionRow

        with self._scope() as db:
            rows = (
                db.query(RuntimeSessionRow)
                .filter_by(user_id=user_id)
                .all()
            )
        return [self._row_to_session(r) for r in rows]

    # ─────────────────────────────────────────────────────────────
    # Metrics — SQL aggregate over `runtime_sessions`.
    # Same wire shape as session's in-memory version.
    # ─────────────────────────────────────────────────────────────
    def metrics(self) -> dict[str, Any]:
        """/ session (SQL-backed). Returns the same
        shape as the pre-persistence in-memory version:
        `total_sessions`, `by_status`, `unique_users`,
        `oldest_session_age_seconds`. Counters and the oldest-age
        are computed via SQL aggregates (cheap on SQLite)."""
        from sqlalchemy import func

        from app.db.models import RuntimeSessionRow

        with self._scope() as db:
            total = (
                db.query(func.count(RuntimeSessionRow.id)).scalar() or 0
            )
            status_counts = (
                db.query(
                    RuntimeSessionRow.status,
                    func.count(RuntimeSessionRow.id),
                )
                .group_by(RuntimeSessionRow.status)
                .all()
            )
            unique_users = (
                db.query(
                    func.count(func.distinct(RuntimeSessionRow.user_id))
                )
                .filter(RuntimeSessionRow.user_id.isnot(None))
                .scalar()
            ) or 0
            oldest_started = (
                db.query(func.min(RuntimeSessionRow.started_at)).scalar()
            )
        now_mono = time.monotonic()
        oldest_age = (
            (now_mono - oldest_started)
            if oldest_started is not None
            else None
        )
        return {
            "total_sessions": total,
            "by_status": dict(status_counts),
            "unique_users": unique_users,
            "oldest_session_age_seconds": oldest_age,
        }

    # ─────────────────────────────────────────────────────────────
    # Internal — row ↔ session mapping, write helpers.
    # ─────────────────────────────────────────────────────────────
    def _row_to_session(self, row) -> RuntimeSession:
        """Map a SQLAlchemy `RuntimeSessionRow` to a fresh
        in-memory `RuntimeSession`. The `wf` field defaults to
        `None`; callers that need a compiled `wf` (continue /
        cancel) recompile via `build_workflow(...)`."""
        return RuntimeSession(
            id=row.id,
            workflow_id=row.workflow_id,
            user_id=row.user_id,
            input=row.input,
            output=row.output,
            status=row.status,
            run_id=row.run_id,
            pending_requirements=list(row.pending_requirements or []),
            node_types=dict(row.node_types or {}),
            history=list(row.history or []),
            started_at=row.started_at,
            last_seen_at=row.last_seen_at,
            workflow_updated_at=row.workflow_updated_at,
        )

    def _write_row(self, sess: RuntimeSession) -> None:
        """Flush: UPDATE the row in place via PK lookup. If the
        row is missing (shouldn't happen in production — `create`
        always INSERTs first), re-create it defensively."""
        from app.db.models import RuntimeSessionRow

        with self._scope() as db:
            row = db.get(RuntimeSessionRow, sess.id)
            if row is None:
                row = RuntimeSessionRow(
                    id=sess.id,
                    workflow_id=sess.workflow_id,
                    user_id=sess.user_id,
                    input=sess.input,
                    workflow_updated_at=sess.workflow_updated_at,
                    started_at=sess.started_at,
                    last_seen_at=sess.last_seen_at,
                )
                db.add(row)
            row.status = sess.status
            row.output = sess.output
            row.run_id = sess.run_id
            # / session (commit 1): persist the JSON-safe
            # VIEW of pending_requirements — the in-memory objects
            # are agno's `StepRequirement` (with `OnTimeout` enums
            # and other non-JSON-serializable fields). The in-process
            # cache retains the real objects for the resume path;
            # the DB only stores a view that survives JSON encode.
            # Full resume-on-restart serialization is commit 2.
            row.pending_requirements = self._serialize_reqs(
                sess.pending_requirements
            )
            row.node_types = dict(sess.node_types or {})
            row.history = list(sess.history or [])
            # `started_at` is normally set at create time and never
            # mutated, but the test suite backdates it to verify the
            # metrics aggregate — flush must keep it in sync.
            row.started_at = sess.started_at
            row.last_seen_at = sess.last_seen_at

    @staticmethod
    def _serialize_reqs(reqs) -> list:
        """JSON-safe view of `pending_requirements`. Walks each
        item recursively: objects with `__dict__` are flattened
        to a dict (Enums → `.value`, lists/tuples/dicts recursed,
        unknown types → `str()`). Leaves JSON-native scalars
        untouched. The in-memory `RuntimeSession` keeps the real
        `StepRequirement` objects for the resume path; only the
        DB column carries the JSON-safe view. Commit 2 adds the
        roundtrip — `_row_to_session` will reconstruct
        `StepRequirement` from the dict for cross-restart resume."""
        def _walk(v):
            if v is None or isinstance(v, (str, int, float, bool)):
                return v
            if hasattr(v, "value") and not isinstance(v, (str, int, float, bool)):
                # Enum → its value.
                return v.value
            if isinstance(v, dict):
                return {k: _walk(val) for k, val in v.items()}
            if isinstance(v, (list, tuple, set, frozenset)):
                return [_walk(item) for item in v]
            d = getattr(v, "__dict__", None)
            if d is not None:
                return {k: _walk(val) for k, val in d.items()}
            return str(v)

        return [_walk(r) for r in (reqs or []) if r is not None]

    def _write_last_seen(self, sess: RuntimeSession) -> None:
        """Cheap single-column UPDATE — used by `get_for_user` on
        every call so the cleanup cron sees an accurate TTL
        window. `get` (the per-event EventAdapter path) skips
        this to keep streaming cheap."""
        from app.db.models import RuntimeSessionRow

        with self._scope() as db:
            row = db.get(RuntimeSessionRow, sess.id)
            if row is not None:
                row.last_seen_at = sess.last_seen_at

def session_store() -> SessionStore:
    return SessionStore.instance()