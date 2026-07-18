"""Unit tests for `app.runtime.session.SessionStore`.

Pin the session-store persistence contract:
  * `cleanup_idle(ttl)` drops sessions whose DB `last_seen_at` is
    older than `ttl` seconds.
  * `cleanup_idle(ttl)` KEEPS sessions in `status == "running"`
    regardless of age (an in-flight agno loop must not be GC'd
    mid-run — the EventAdapter needs the slim session's
    `run_id` to translate events).
  * `last_seen_at` is refreshed on `get_for_user` (writes to DB)
    and on `get` cache hits (in-memory only — EventAdapter's
    per-event reads don't carry the "user is interacting"
    signal).
  * Cache miss on either path reads through SQLite and writes
    the refreshed `last_seen_at` back, so post-restart reads
    extend the TTL window.
  * `RuntimeSession.flush()` mirrors in-memory state to the DB
    row (called at well-defined service-layer checkpoints).

Run:  PYTHONPATH=src .venv/bin/python -m pytest tests/test_session_store.py -v
"""
from __future__ import annotations

import time

import pytest

from app.runtime.session import RuntimeSession, SessionStore

@pytest.fixture()
def store() -> SessionStore:
    """Fresh SessionStore per test — bypass `SessionStore.instance()`
    so tests don't fight the process-wide singleton. The conftest
    autouse fixture `_bind_session_store_engine` already swapped
    `SessionStore._engine` to the per-test in-memory SQLite, so
    even the direct `SessionStore()` constructor uses the test DB."""
    return SessionStore()

def _make_idle_sess(store, *, user_id="alice", workflow_id="wf-1", age_seconds=3600):
    """Helper: create a session and mark it idle past TTL by
    backdating `last_seen_at` AND flipping status out of 'running'
    (the cleanup never touches in-flight runs per the carve-out).

    Session-store: flushes the backdated values to SQLite so
    `cleanup_idle` (which sweeps the DB) sees them."""
    sess = store.create(workflow_id, "hi", user_id=user_id)
    sess.status = "completed"
    sess.last_seen_at = time.monotonic() - age_seconds
    sess.flush()
    return sess

# ─────────────────────────────────────────────────────────────────
# cleanup_idle — happy path
# ─────────────────────────────────────────────────────────────────
def test_cleanup_idle_drops_sessions_past_ttl(store):
    sess = _make_idle_sess(store)
    dropped = store.cleanup_idle(ttl_seconds=60.0)
    assert dropped == 1
    assert store.get(sess.id) is None

def test_cleanup_idle_keeps_sessions_within_ttl(store):
    sess = store.create("wf-1", "hi", user_id="alice")
    dropped = store.cleanup_idle(ttl_seconds=60.0)
    assert dropped == 0
    assert store.get(sess.id) is sess

def test_cleanup_idle_keeps_in_flight_run_regardless_of_age(store):
    """Pin the session-store carve-out: status == "running" is
    NEVER dropped. The slim session is the EventAdapter's
    only handle on the live Wf.run; killing it mid-stream
    would orphan the SSE response."""
    sess = store.create("wf-1", "hi", user_id="alice")
    sess.status = "running"
    sess.last_seen_at = time.monotonic() - 3600
    sess.flush()  # persist the backdated last_seen_at
    dropped = store.cleanup_idle(ttl_seconds=60.0)
    assert dropped == 0
    assert store.get(sess.id) is sess

def test_cleanup_idle_drops_completed_sessions(store):
    sess = _make_idle_sess(store, user_id="alice")
    dropped = store.cleanup_idle(ttl_seconds=60.0)
    assert dropped == 1

def test_cleanup_idle_zero_ttl_is_noop(store):
    """Defensive: ttl<=0 disables cleanup. Useful for tests that
    want to opt out without setting AGNOBUILDER_SKIP_CLEANUP."""
    sess = _make_idle_sess(store)
    dropped = store.cleanup_idle(ttl_seconds=0)
    assert dropped == 0
    assert store.get(sess.id) is sess

# ─────────────────────────────────────────────────────────────────
# last_seen_at refresh
# ─────────────────────────────────────────────────────────────────
def test_get_refreshes_last_seen_at_in_memory(store):
    """`get` (unscoped) refreshes in-memory `last_seen_at` on
    cache hit. It does NOT write to SQLite — the EventAdapter
    uses `get` per event during a stream, and per-event DB
    writes would dominate the streaming cost without buying
    anything (TTL extension is a "user is interacting" signal
    that per-event reads don't carry). The in-memory refresh
    is what `_run_leg`'s reuse branch uses to keep the session
    alive within a single leg."""
    sess = _make_idle_sess(store)
    sess.last_seen_at = time.monotonic() - 3600
    store.get(sess.id)
    assert time.monotonic() - sess.last_seen_at < 1.0

def test_get_for_user_refreshes_last_seen_at_persists(store):
    """`get_for_user` (owner-scoped, the production HTTP path)
    writes the refreshed `last_seen_at` to DB so the cleanup
    cron sees an accurate TTL window. Without this write, an
    actively-used session would be silently dropped past TTL
    even though the user is still hitting the API."""
    sess = _make_idle_sess(store)
    sess.last_seen_at = time.monotonic() - 3600
    store.get_for_user(sess.id, "alice")
    assert time.monotonic() - sess.last_seen_at < 1.0
    dropped = store.cleanup_idle(ttl_seconds=60.0)
    assert dropped == 0

def test_cache_miss_writes_last_seen_to_db(store):
    """Pin session-store design D: when the cache is
    empty (e.g. immediately after a process restart), `get`
    reads through SQLite AND writes the refreshed `last_seen_at`
    back. Without this write, a paused session that's the
    target of the first post-restart fetch would have its DB
    `last_seen_at` stale, and the next cleanup tick (60s later)
    would drop the row the user was about to resume."""
    sess = _make_idle_sess(store)
    sess.last_seen_at = time.monotonic() - 3600
    sess.flush()
    # Simulate a process restart: drop the cache.
    store._cache.clear()
    # First read populates the cache AND writes back last_seen_at.
    store.get(sess.id)
    # Cleanup with TTL=60s should keep it (last_seen_at is now
    # fresh in DB, not the backdated 1h-old value).
    dropped = store.cleanup_idle(ttl_seconds=60.0)
    assert dropped == 0

# ─────────────────────────────────────────────────────────────────
# Mixed-state cleanup
# ─────────────────────────────────────────────────────────────────
def test_cleanup_idle_drops_only_stale_rows(store):
    """Cleanup is per-row, not all-or-nothing. 3 sessions:
    one fresh, one idle, one running (ancient). After cleanup:
    1 dropped (the idle), 2 remain."""
    fresh = store.create("wf-1", "fresh", user_id="alice")
    idle = _make_idle_sess(store, user_id="alice")
    running = store.create("wf-1", "running", user_id="bob")
    running.status = "running"
    running.last_seen_at = time.monotonic() - 3600
    running.flush()
    dropped = store.cleanup_idle(ttl_seconds=60.0)
    assert dropped == 1
    assert store.get(fresh.id) is fresh
    assert store.get(running.id) is running
    assert store.get(idle.id) is None

def test_cleanup_idle_evicts_cache(store):
    """Session cleanup: cleanup removes the row from BOTH the DB
    and the in-memory cache. A test that holds onto a stale
    reference would otherwise think the session still exists
    until the next process restart."""
    sess = _make_idle_sess(store, user_id="alice")
    assert sess.id in store._cache
    store.cleanup_idle(ttl_seconds=60.0)
    assert sess.id not in store._cache

# ─────────────────────────────────────────────────────────────────
# Operational metrics (SQL-backed)
# ─────────────────────────────────────────────────────────────────
def test_metrics_empty_store(store):
    """Fresh store — no sessions. metrics() returns the empty
    shape (total=0, by_status={}, oldest_session_age_seconds=None)."""
    m = store.metrics()
    assert m["total_sessions"] == 0
    assert m["by_status"] == {}
    assert m["unique_users"] == 0
    assert m["oldest_session_age_seconds"] is None

def test_metrics_counts_status_breakdown(store):
    """3 sessions across 2 statuses — total=3, by_status
    matches the per-status counts. This is the data an admin
    panel renders as a bar chart."""
    _make_idle_sess(store, user_id="alice")  # completed
    s2 = store.create("wf-1", "x", user_id="alice")
    s2.status = "completed"
    s2.flush()
    s3 = store.create("wf-1", "x", user_id="bob")
    s3.status = "error"
    s3.flush()
    m = store.metrics()
    assert m["total_sessions"] == 3
    assert m["by_status"]["completed"] == 2
    assert m["by_status"]["error"] == 1
    # 'running' never appeared — pin the missing-key shape.
    assert "running" not in m["by_status"]

def test_metrics_counts_unique_users(store):
    """unique_users is the count of distinct non-null
    `user_id` values across the table (an SQL `COUNT(DISTINCT)`).
    Anonymous sessions (`user_id IS NULL`) are excluded — they
    don't carry an owner identity the admin panel could
    enumerate."""
    # Anonymous user — no user_id stored.
    store.create("wf-1", "x", user_id=None)
    # Two named users.
    store.create("wf-1", "x", user_id="alice")
    store.create("wf-1", "x", user_id="alice")
    store.create("wf-1", "x", user_id="bob")
    m = store.metrics()
    # Anonymous doesn't count; alice + bob → 2.
    assert m["unique_users"] == 2

def test_metrics_oldest_session_age(store):
    """oldest_session_age_seconds is the time since the
    earliest `started_at` in the table. SQL aggregate
    `MIN(started_at)`; the age is computed in Python as
    `now_mono - min`. Backdating `started_at` requires
    `flush()` so the DB sees the new value."""
    s1 = store.create("wf-1", "x", user_id="alice")
    s1.started_at = time.monotonic() - 200
    s1.flush()
    s2 = store.create("wf-1", "x", user_id="bob")
    s2.started_at = time.monotonic() - 100
    s2.flush()
    m = store.metrics()
    assert m["oldest_session_age_seconds"] is not None
    # Oldest is s1 (started 200s ago). Allow a 1s window.
    assert 199 < m["oldest_session_age_seconds"] < 201

# ─────────────────────────────────────────────────────────────────
# Persistence contract
# ─────────────────────────────────────────────────────────────────
def test_create_persists_row_to_db(store):
    """`create` INSERTs a row in `runtime_sessions`. A raw SQL
    query after `create` sees the row, not just the cache."""
    sess = store.create("wf-1", "hello", user_id="alice")
    with store._scope() as db:
        from app.db.models import RuntimeSessionRow

        row = db.get(RuntimeSessionRow, sess.id)
    assert row is not None
    assert row.id == sess.id
    assert row.workflow_id == "wf-1"
    assert row.input == "hello"
    assert row.user_id == "alice"

def test_flush_mirrors_status_to_db(store):
    """In-memory mutations on `sess.status` aren't visible to
    `metrics()` until `flush()` runs. The runtime service calls
    `flush()` at the end of `_finalise_leg` for exactly this
    reason."""
    sess = store.create("wf-1", "x", user_id="alice")
    # Without flush, the DB still has status="running".
    sess.status = "completed"
    sess.flush()
    m = store.metrics()
    assert m["by_status"]["completed"] == 1
    assert "running" not in m["by_status"]

def test_session_survives_cache_clear(store):
    """Pin the session-store restart contract: after a
    process restart (simulated by clearing the in-memory
    cache), the session is still loadable from SQLite.

    This is the test that proves the migration actually
    delivers durability — without it, the migration is a
    no-op dressed up as a refactor."""
    sess = store.create("wf-1", "hello", user_id="alice")
    sess.status = "completed"
    sess.output = "done"
    sess.flush()
    # Simulate process restart.
    store._cache.clear()
    # First read populates the cache from SQLite.
    rehydrated = store.get(sess.id)
    assert rehydrated is not None
    assert rehydrated.id == sess.id
    assert rehydrated.workflow_id == "wf-1"
    assert rehydrated.input == "hello"
    assert rehydrated.status == "completed"
    assert rehydrated.output == "done"
    assert rehydrated.user_id == "alice"

def test_delete_removes_db_row(store):
    """`delete` removes the row from SQLite (not just the
    cache). The cross-restart contract — sessions persist
    unless explicitly deleted — depends on this."""
    sess = store.create("wf-1", "x", user_id="alice")
    store.delete(sess.id)
    with store._scope() as db:
        from app.db.models import RuntimeSessionRow

        row = db.get(RuntimeSessionRow, sess.id)
    assert row is None
    # And the cache is also evicted.
    assert sess.id not in store._cache

def test_get_for_user_cross_user_returns_none(store):
    """Owner-scoped read: `get_for_user` enforces ownership.
    A cross-user probe returns None, the API turns that into
    404 (same shape as "no such session" — non-owners can't
    enumerate sids by status code)."""
    sess = store.create("wf-1", "x", user_id="alice")
    sess.flush()
    assert store.get_for_user(sess.id, "bob") is None
    # Anonymous↔anonymous back-compat still works.
    anon = store.create("wf-2", "x", user_id=None)
    assert store.get_for_user(anon.id, None) is anon