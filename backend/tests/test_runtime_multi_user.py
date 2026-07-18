"""Multi-user isolation tests for the runtime SSE channel.

Caller verification follow-up: `/runtime/continue` and
`/runtime/sessions/{id}` previously had no caller verification.
Any client that knew (or guessed) an 8-char sid could resume or
read another user's session. These tests pin the fix:

  * Alice starts a paused workflow run.
  * Bob (different `X-User-Id`) cannot resume Alice's session.
  * Bob cannot read Alice's session via `GET /sessions/{sid}`.
  * Alice can still resume + read her own session.

The wire shape for cross-user attempts is deliberately
indistinguishable from "no such session" — same 404 — so a
non-owner can't enumerate sids by status code.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

ALICE = {"X-User-Id": "alice@example.com"}
BOB = {"X-User-Id": "bob@example.com"}

def _parse_sse(body: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for chunk in body.split(b"\n\n"):
        for line in chunk.splitlines():
            if line.startswith(b"data:"):
                payload = line[len(b"data:"):].lstrip()
                if payload == b"[DONE]":
                    return events
                events.append(json.loads(payload))
    return events

@pytest.fixture()
def paused_session(client) -> str:
    """Create a workflow + run it under Alice until it pauses for
    human input. Returns the session id (the value of
    `X-Session-Id` in the run response)."""
    r = client.post(
        "/api/v1/workflows",
        json={
            "name": "ask-and-wait",
            "nodes": [
                {
                    "id": "ask",
                    "type": "human_input",
                    "position": {"x": 100, "y": 0},
                    "data": {
                        "label": "Ask",
                        "config": {"prompt": "Your name?", "inputType": "text"},
                    },
                },
            ],
            "edges": [],
        },
        headers=ALICE,
    )
    assert r.status_code == 201
    wf_id = r.json()["id"]

    r = client.post(
        "/api/v1/runtime/run",
        json={"workflow_id": wf_id, "input": "x"},
        headers=ALICE,
    )
    assert r.status_code == 200
    sid = r.headers.get("x-session-id")
    assert sid is not None, "run response should carry X-Session-Id header"
    # Sanity: the run actually paused (we want a session that's
    # waiting_confirmation, so resume would otherwise succeed).
    events = _parse_sse(r.content)
    visible = [e for e in events if e["type"] not in ("node_start", "node_end")]
    assert visible and visible[-1]["type"] == "confirmation"
    return sid

def test_continue_rejects_other_user(client, paused_session):
    """Bob (different X-User-Id) cannot resume Alice's paused session."""
    sid = paused_session
    r = client.post(
        "/api/v1/runtime/continue",
        json={"session_id": sid, "response": "Mallory"},
        headers=BOB,
    )
    # Wire shape deliberately matches "no such session" so a
    # non-owner can't distinguish "doesn't exist" from "isn't yours".
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()

def test_continue_accepts_owner(client, paused_session):
    """Sanity check: Alice can still resume her own session."""
    sid = paused_session
    r = client.post(
        "/api/v1/runtime/continue",
        json={"session_id": sid, "response": "Alice"},
        headers=ALICE,
    )
    assert r.status_code == 200
    events = _parse_sse(r.content)
    visible = [e for e in events if e["type"] not in ("node_start", "node_end")]
    assert visible[-1]["type"] == "completed"

def test_session_get_rejects_other_user(client, paused_session):
    """Bob cannot read Alice's session history via GET."""
    sid = paused_session
    r = client.get(f"/api/v1/runtime/sessions/{sid}", headers=BOB)
    assert r.status_code == 404

def test_session_get_accepts_owner(client, paused_session):
    """Sanity check: Alice can read her own session."""
    sid = paused_session
    r = client.get(f"/api/v1/runtime/sessions/{sid}", headers=ALICE)
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == sid
    assert body["status"] == "waiting_confirmation"

def test_concurrent_runs_isolated_by_user(client, seeded_default_preset):
    """Alice's run and Bob's run must not share state.

    Alice starts a workflow that pauses; Bob starts an identical
    workflow that pauses. Each must receive its OWN sid; neither
    can resume the other's session; the SessionStore must hold
    them in separate per-user buckets.
    """
    # Alice creates a workflow + pauses it
    r = client.post(
        "/api/v1/workflows",
        json={
            "name": "alice-wf",
            "nodes": [
                {"id": "ask", "type": "human_input", "position": {"x": 0, "y": 0},
                 "data": {"label": "Ask", "config": {"prompt": "?", "inputType": "text"}}},
            ],
            "edges": [],
        },
        headers=ALICE,
    )
    assert r.status_code == 201
    alice_wf = r.json()["id"]

    r = client.post(
        "/api/v1/runtime/run",
        json={"workflow_id": alice_wf, "input": "x"},
        headers=ALICE,
    )
    assert r.status_code == 200
    alice_sid = r.headers["x-session-id"]

    # Bob creates his own + pauses it
    r = client.post(
        "/api/v1/workflows",
        json={
            "name": "bob-wf",
            "nodes": [
                {"id": "ask", "type": "human_input", "position": {"x": 0, "y": 0},
                 "data": {"label": "Ask", "config": {"prompt": "?", "inputType": "text"}}},
            ],
            "edges": [],
        },
        headers=BOB,
    )
    assert r.status_code == 201
    bob_wf = r.json()["id"]

    r = client.post(
        "/api/v1/runtime/run",
        json={"workflow_id": bob_wf, "input": "y"},
        headers=BOB,
    )
    assert r.status_code == 200
    bob_sid = r.headers["x-session-id"]

    # Distinct sids (8-hex collision is astronomically unlikely)
    assert alice_sid != bob_sid

    # Bob's resume with Alice's sid → 404
    r = client.post(
        "/api/v1/runtime/continue",
        json={"session_id": alice_sid, "response": "x"},
        headers=BOB,
    )
    assert r.status_code == 404

    # Alice's resume with Bob's sid → 404
    r = client.post(
        "/api/v1/runtime/continue",
        json={"session_id": bob_sid, "response": "x"},
        headers=ALICE,
    )
    assert r.status_code == 404

    # The store's per-user partitioning shows two distinct buckets.
    from app.runtime.session import session_store

    store = session_store()
    alice_bucket = {s.id for s in store.list_for_user("alice@example.com")}
    bob_bucket = {s.id for s in store.list_for_user("bob@example.com")}
    assert alice_sid in alice_bucket
    assert bob_sid in bob_bucket
    # No cross-contamination between buckets.
    assert alice_sid not in bob_bucket
    assert bob_sid not in alice_bucket

def test_session_user_id_is_persisted(client, seeded_default_preset):
    """A freshly created session carries the caller's user_id, so
    later ownership checks can match against it without a separate
    keyspace."""
    r = client.post(
        "/api/v1/workflows",
        json={
            "name": "owned",
            "nodes": [
                {"id": "ag", "type": "agent", "position": {"x": 0, "y": 0},
                 "data": {"label": "Bot", "config": {}}},
            ],
            "edges": [],
        },
        headers=ALICE,
    )
    wf_id = r.json()["id"]
    r = client.post(
        "/api/v1/runtime/run",
        json={"workflow_id": wf_id, "input": "x"},
        headers=ALICE,
    )
    sid = r.headers["x-session-id"]

    from app.runtime.session import session_store

    sess = session_store().get(sid)
    assert sess is not None
    assert sess.user_id == "alice@example.com"

def test_anonymous_default_user_cannot_read_named_user_session(
    client, paused_session
):
    """The `user-default` placeholder (no X-User-Id header) must
    not be able to read a real user's session — anonymous↔named
    pairing is NOT allowed (only anonymous↔anonymous, which is
    smoke-test territory)."""
    sid = paused_session
    r = client.get(f"/api/v1/runtime/sessions/{sid}")  # no header
    assert r.status_code == 404

def test_anonymous_default_user_cannot_resume_named_user_session(
    client, paused_session
):
    """Same shape, for the resume endpoint."""
    sid = paused_session
    r = client.post(
        "/api/v1/runtime/continue",
        json={"session_id": sid, "response": "x"},
    )  # no header
    assert r.status_code == 404

# ─────────────────────────────────────────────────────────────────
# / session : ownership on the /run REUSE path.
# ─────────────────────────────────────────────────────────────────
def test_run_reuse_with_other_users_session_id_mints_fresh(
    client, seeded_default_preset
):
    """session regression: Bob (different X-User-Id) POSTs to /run
    on his OWN workflow but carrying Alice's `session_id` in
    the body. The pre-fix code used `store.get(session_id)`
    (unscoped), so Bob's request would have REUSED Alice's
    slim session and run her conversation under Bob's user_id.
    With the fix, the reuse branch uses
    `get_for_user(session_id, user_id)` which returns None for
    cross-user attempts, the runtime falls through to
    `create(...)`, and Bob gets a FRESH slim session surfaced
    via X-Session-Id (different from Alice's). Alice's
    conversation is untouched.

    NB: this test sets up the cross-user case at the SESSION
    layer, not the WORKFLOW layer — Bob owns his own workflow
    (so the workflow-access check passes) and tries to attach
    to Alice's existing session via the `session_id` field. The
    workflow-access check would 403 Bob first; we route around
    it by giving Bob his own workflow. The session-ownership
    check is the layer under test here.

    This is the same wire shape as a stale-session_id retry:
    the runtime can't tell the two apart by the time it does
    the lookup, but the SECURITY partition is what matters
    — the cross-user read can never succeed, period."""
    # Alice creates a real workflow + runs it. The run completes
    # immediately (single agent node) but the slim session is
    # still created in the process — the X-Session-Id header
    # carries it.
    r_create = client.post(
        "/api/v1/workflows",
        json={
            "name": "alice-single-agent",
            "nodes": [
                {"id": "a", "type": "agent", "position": {"x": 0, "y": 0},
                 "data": {"label": "A", "config": {}}},
            ],
            "edges": [],
        },
        headers=ALICE,
    )
    assert r_create.status_code == 201
    alice_wf = r_create.json()["id"]
    r_alice = client.post(
        "/api/v1/runtime/run",
        json={"workflow_id": alice_wf, "input": "hi"},
        headers=ALICE,
    )
    assert r_alice.status_code == 200
    alice_sid = r_alice.headers.get("X-Session-Id")
    assert alice_sid, "runtime did not surface X-Session-Id for Alice"

    # Bob creates his OWN workflow (so the workflow-access check
    # passes — that's not the layer under test). Then he tries
    # to attach to Alice's session via the `session_id` field.
    r_bob_wf = client.post(
        "/api/v1/workflows",
        json={
            "name": "bob-single-agent",
            "nodes": [
                {"id": "a", "type": "agent", "position": {"x": 0, "y": 0},
                 "data": {"label": "A", "config": {}}},
            ],
            "edges": [],
        },
        headers=BOB,
    )
    assert r_bob_wf.status_code == 201
    bob_wf = r_bob_wf.json()["id"]

    r_bob = client.post(
        "/api/v1/runtime/run",
        json={
            "workflow_id": bob_wf,
            "input": "I'm Bob, hi",
            "session_id": alice_sid,
        },
        headers=BOB,
    )
    # Bob gets a 200 (the request was valid for HIS workflow)
    # but a FRESH sid — not Alice's. He did NOT resume Alice's
    # session.
    assert r_bob.status_code == 200
    bob_sid = r_bob.headers.get("X-Session-Id")
    assert bob_sid, "runtime did not surface X-Session-Id for Bob"
    assert bob_sid != alice_sid, (
        "session ownership fix broken — Bob reused Alice's session id. "
        "Cross-user reads of the slim session are a critical "
        "issue; the partition MUST hold."
    )

def test_run_first_create_marks_session_owner(
    client, seeded_default_preset
):
    """session: the `user_id` carried on the slim session
    (`RuntimeSession.user_id`) is what `get_for_user` checks
    against on the next /run. Pin that the FIRST create stamps
    the caller's user_id so a subsequent /run from a different
    user can't pass the ownership check via a stale-uid-omitted
    slim session. (Anonymous↔anonymous is allowed per the
    partition in `runtime/session.py`; the test exercises the
    NAMED-user path.)"""
    from app.runtime.session import session_store

    # Alice creates a workflow + runs it once.
    r = client.post(
        "/api/v1/workflows",
        json={
            "name": "single-agent",
            "nodes": [
                {"id": "a", "type": "agent", "position": {"x": 0, "y": 0},
                 "data": {"label": "A", "config": {}}},
            ],
            "edges": [],
        },
        headers=ALICE,
    )
    assert r.status_code == 201
    wf_id = r.json()["id"]
    r_run = client.post(
        "/api/v1/runtime/run",
        json={"workflow_id": wf_id, "input": "hi"},
        headers=ALICE,
    )
    sid = r_run.headers.get("X-Session-Id")
    assert sid
    sess = session_store().get(sid)
    assert sess is not None
    assert sess.user_id == "alice@example.com", (
        "slim session was created without the caller's user_id — "
        "the ownership partition relies on this field being set "
        "at create-time so `get_for_user` can enforce it later"
    )