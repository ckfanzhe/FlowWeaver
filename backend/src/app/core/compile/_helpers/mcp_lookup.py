"""MCP server lookup for the export path.

The generator writes Python that re-instantiates an `MCPTools(...)` per
node. The actual `command` / `args` / `url` lives in the SQLite `McpServer`
table — the generator reads it during rendering and inlines the values
into the exported source.

The lookup is **best-effort**: if the DB query fails (table missing in a
test fixture, server row deleted between save and export), we return
`(None, None, None)` and let the emitter emit a placeholder. The export
must never fail just because an optional side-table can't be read.

 (round 2) — strict per-user binding. `mcp_target_for_export`
accepts an optional `user_id`; when provided, the lookup is strictly
scoped to `(user_id = <X>)`. The shared system tier (`user_id IS NULL`)
is gone — every user configures their own MCP servers. The export
endpoint threads the workflow owner's id (`workflow.created_by`)
through so a shared workflow can't leak an owner's private MCP server
into someone else's download.
"""
from __future__ import annotations

def mcp_target_for_export(
    server_id: str,
    user_id: str | None = None,
) -> tuple[str | None, list[str] | None, str | None]:
    """Look up the MCP server config so the export can wire the real command/url.

    Returns `(command, args, url)`. For stdio: command+args; for sse: url.
    Falls back to `(None, None, None)` if the server isn't found (the
    emitter decides how to render the placeholder).

    `user_id` ( round 2): scopes the lookup to the caller's
    rows strictly. `None` keeps the pre-binding behaviour (any visible
    row) — used by callers that don't have a caller identity (e.g.
    background renderers, most unit tests).
    """
    if not server_id:
        return None, None, None
    try:
        from app.db.models import McpServer
        from app.db.session import session_scope
    except Exception:  # noqa: BLE001
        return None, None, None
    try:
        with session_scope() as db:
            q = db.query(McpServer).filter_by(id=server_id)
            if user_id is not None:
                q = q.filter(McpServer.user_id == user_id)
            row = q.one_or_none()
            if row is None:
                return None, None, None
            if row.transport == "sse":
                return None, None, row.url
            return row.command, list(row.args or []), None
    except Exception:  # noqa: BLE001
        return None, None, None