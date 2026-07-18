"""Regression tests for CORS header exposure on the runtime SSE endpoints.

Why this file exists:
  The frontend's chat store reads `X-Session-Id` from the SSE response
  headers to know which session a `human_input` pause belongs to. CORS
  hides non-safelisted response headers from JS unless the server
  explicitly lists them in `Access-Control-Expose-Headers`. When this
  list was empty, the browser returned `null` from
  `res.headers.get('x-session-id')`, the chat store never persisted the
  session id, and the dispatcher routed every user "answer" as a fresh
  `send()` — which re-entered the same `human_input` pause. Loop.

These tests assert the server SENDS the right `Access-Control-Expose-Headers`
on the runtime endpoints. The browser then knows it can read `X-Session-Id`.

(curl ignores CORS, which is why the bug stayed invisible to manual
curl reproductions; TestClient also ignores CORS but DOES surface all
response headers, so this test reproduces the server-side fix.)
"""
from __future__ import annotations

def _parse_expose(value: str | None) -> set[str]:
    if not value:
        return set()
    return {h.strip().lower() for h in value.split(",") if h.strip()}

def test_cors_exposes_x_session_id_on_runtime_run(client):
    """A simulated browser request must see X-Session-Id exposed.

    We don't need to actually run a workflow — we just need to confirm
    the CORS middleware advertises the header on responses from the
    runtime endpoints. A real OPTIONS preflight is overkill; any
    response from `/runtime/run` will do.
    """
    # Make a real POST — even one that 404s carries the CORS headers
    # (CORSMiddleware runs before the route handler).
    r = client.post(
        "/api/v1/runtime/run",
        json={"workflow_id": "nope", "input": "x"},
        headers={"Origin": "http://localhost:5173"},
    )
    # The body is irrelevant — we only care about the headers.
    assert r.status_code in (404, 422)  # 404 (no such workflow) or 422 (validation)
    exposed = _parse_expose(r.headers.get("access-control-expose-headers"))
    assert "x-session-id" in exposed, (
        f"X-Session-Id must be CORS-exposed for the frontend chat store to "
        f"track paused sessions. Got: {sorted(exposed)}"
    )

def test_cors_exposes_x_session_id_on_runtime_continue(client):
    """The continue endpoint must also expose X-Session-Id (the response
    header is reused there for symmetry with `/run`)."""
    r = client.post(
        "/api/v1/runtime/continue",
        json={"session_id": "nope", "response": "x"},
        headers={"Origin": "http://localhost:5173"},
    )
    assert r.status_code in (404, 409)
    exposed = _parse_expose(r.headers.get("access-control-expose-headers"))
    assert "x-session-id" in exposed

def test_cors_options_preflight_advertises_runtime_route(client):
    """A real CORS preflight must come back OK with the right
    allow-headers so a fetch with custom Content-Type is allowed."""
    r = client.options(
        "/api/v1/runtime/run",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 200
    allow_methods = r.headers.get("access-control-allow-methods", "")
    assert "POST" in allow_methods
    # The preflight itself doesn't expose X-Session-Id (that's for
    # responses, not preflight), but the middleware should at least
    # answer cleanly.