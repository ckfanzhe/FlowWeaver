"""SQLAlchemy ORM models."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

class Workflow(Base):
    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    nodes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    edges: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Built-in templates (seeded by `_seed_templates()` on first startup).
    # `is_template=True` rows are read-only via the public API — the
    # frontend uses them as a "new from template" menu but can't
    # PUT/PATCH/DELETE them. `category` groups templates in the gallery
    # (e.g. "starter", "branching", "loop", "tools").
    is_template: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    # Locale tag (added ). Mirrors the value in the JSON's
    # `locale` field — the seed reads it from the loader entry and
    # stores it here so the API can filter the gallery by language
    # without re-parsing the JSON files. Default `"en"` keeps every
    # existing user row on the English version.
    locale: Mapped[str] = mapped_column(
        String, nullable=False, default="en", server_default="en",
    )
    # Creator's `user_id` (the X-User-Id header at insert time, or
    # `"user-default"` when none was supplied). The single-engine
    # refactor deliberately left this out so the previous "single
    # owner" model held; the multi-user work adds it back so list /
    # permission lookups can scope by it without a join. `tenant_id`
    # is reserved for the future multi-tenant login layer — today
    # every row is `"tenant-default"` and we ignore it for filtering,
    # but the column is here so the auth swap doesn't need a
    # migration.
    created_by: Mapped[str] = mapped_column(
        String, nullable=False, default="user-default", server_default="user-default",
    )
    tenant_id: Mapped[str] = mapped_column(
        String, nullable=False, default="tenant-default", server_default="tenant-default",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

class McpServer(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    transport: Mapped[str] = mapped_column(String, nullable=False)  # "stdio" | "sse"
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # stdio fields
    command: Mapped[str | None] = mapped_column(String, nullable=True)
    args: Mapped[list | None] = mapped_column(JSON, nullable=True)
    env: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # sse fields
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    headers: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Per-user binding : NULL = system row (shared,
    # read-only via the API), non-NULL = the `users.id` of the owning
    # user. The list endpoint returns the caller's rows + system
    # rows; mutations are owner-only.
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

class LlmPreset(Base):
    """A named LLM model the user has pre-configured.

    `api_key` and `base_url` are stored per-row. The platform's runtime
    reads these directly via `app.core.llm_runner.build_model` — there
    is no `.env.llm` fallback. Users who want a zero-config default
    should set up one preset with `is_default=True`.
    """
    __tablename__ = "llm_presets"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)  # openai|anthropic|ollama|google
    model_id: Mapped[str] = mapped_column(String, nullable=False)
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_url: Mapped[str | None] = mapped_column(String, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # P3 : per-preset "thinking mode" toggle. When True,
    # `app.core.llm_runner.build_model` adds the provider-specific
    # reasoning kwargs to the agno `Model` it constructs. Default False
    # — opt-in per preset (the Settings drawer has a button-style
    # toggle per row) instead of a single global preference.
    thinking: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Per-call sampling / length knobs. NULL = "use the model's own
    # default" — `build_model` only forwards these to the agno Model
    # constructor when the value is non-NULL. Range validators live on
    # the Pydantic schemas (`LlmPresetCreate` / `LlmPresetUpdate`);
    # the DB itself doesn't enforce ranges because the placeholder
    # NULL already encodes "unset".
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    top_p: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Per-user binding : NULL = system row (shared,
    # read-only via the API), non-NULL = the `users.id` of the owning
    # user. The `is_default` flag is also per-user: at most one row
    # owned by a given user holds it at a time, but two different
    # users can each have their own default independently.
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

# ─────────────────────────────────────────────────────────────────
# — multi-user / collaboration (added )
#
# Two new tables:
#
#   `users`                — identity row per `X-User-Id`. Today this is
#                            populated lazily (first-seen header value)
#                            so the API can address users without a real
#                            login. Tomorrow's multi-tenant auth
#                            provider will pre-seed rows.
#
#   `workflow_members`     — `(workflow_id, user_id)` × role. The
#                            creator of a workflow gets an `"owner"`
#                            row at insert time; subsequent members are
#                            added via the share endpoint.
#
# `tenant_id` is reserved on BOTH tables so the future multi-tenant
# login layer can scope queries without a schema change. Today every
# row carries `"tenant-default"` and the field is unused for filtering.
# ─────────────────────────────────────────────────────────────────
class User(Base):
    """Lightweight identity row.

    keeps auth out of scope — there's no password, no OAuth. The
    frontend (or curl) sends an `X-User-Id` header on every request
    and the backend looks the user up here. For human users the
    header value IS the email (the `users/identify` endpoint stamps
    `id = email` so a fresh tab can recover its workflow list by
    re-typing the email on a new device).

    The `user-default` placeholder survives for back-compat (curl,
    tests, scripts that don't care about identity) — it has no
    `email`, and it does NOT own any workflows (the create-workflow
    path always uses the caller's identity, defaulting to this row
    only when no header is supplied).

    The real auth layer (when it lands) will:
      1. Issue a verified `user_id` claim (e.g. JWT `sub`) and stamp
         the row's `email` from the upstream IdP.
      2. Populate `tenant_id` from the IdP's tenant claim so all
         subsequent queries can scope to a single tenant.

    Today's code path is intentionally the same shape — only the
    values change.
    """
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # Email — for human users this equals `id`. Nullable so the
    # `user-default` placeholder row stays clean.
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    tenant_id: Mapped[str] = mapped_column(
        String, nullable=False, default="tenant-default", server_default="tenant-default",
    )
    # Preferred UI language . Set by the identify endpoint
    # when the frontend identifies itself; the frontend reads it back
    # via `/users/me` on the next visit and applies the locale before
    # rendering. NULL means "no preference yet" — the browser's
    # localStorage locale (or the platform default) wins.
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Cartoon avatar the user picked from the UserMenu popover. A
    # short opaque id (e.g. `"fox"`, `"robot"`); the frontend maps it
    # to a colored circle + emoji. NULL means "auto-derive from email
    # hash" — the same default appears for any unsaved user.
    avatar_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # UI theme preference . Values: `"light"`, `"dark"`,
    # `"system"`. NULL means "no preference stored yet" — the
    # frontend falls back to localStorage (`agnobuilder.theme`) or
    # the platform default. Persisted per-user so the choice travels
    # with the user across browsers, mirroring how `language` is
    # already bound to the user row.
    theme: Mapped[str | None] = mapped_column(String(8), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

class WorkflowMember(Base):
    """A user's role on a single workflow.

    Composite unique on `(workflow_id, user_id)` — one role per
    (workflow, user) tuple. `tenant_id` mirrors the workflow's tenant
    (denormalised on purpose) so the future multi-tenant layer can
    scope membership lookups with a single index.

    Cascade: deleting a `Workflow` deletes its members; deleting a
    `User` is intentionally NOT cascaded — the API doesn't expose user
    deletion yet, and silently stripping a workflow's last owner
    would leave the row orphaned.
    """
    __tablename__ = "workflow_members"
    __table_args__ = (
        UniqueConstraint("workflow_id", "user_id", name="uq_workflow_member"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Role vocabulary is locked down via the Pydantic `Role` enum on
    # `app.schemas.member`; the DB stores it as a plain string so a
    # future schema migration (e.g. adding `commenter`) doesn't require
    # an ALTER TABLE.
    role: Mapped[str] = mapped_column(String, nullable=False)
    tenant_id: Mapped[str] = mapped_column(
        String, nullable=False, default="tenant-default", server_default="tenant-default",
    )
    invited_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

# ─────────────────────────────────────────────────────────────────
# / session  — slim RuntimeSession persistence
#
# The slim `RuntimeSession` is the orchestrator-side handle that the
# FastAPI runtime uses to carry per-run state across pause / resume
# legs and across HTTP requests: `status`, `output`, `history`,
# `pending_requirements`, `node_types`, `last_seen_at`,
# `workflow_updated_at`. It is deliberately separate from agno's own
# `WorkflowSession` (which is already persisted via
# `app/core/session_bridge.py::BaseDb` — `runs` carries the agno
# side: messages, tool calls, the `WorkflowRunOutput` agno stores
# at each pause).
#
# Pre-session: this lived in `app/runtime/session.py::_sessions`
# — an in-memory dict on a process-wide singleton. A process
# restart cleared the entire store. With this table, the slim
# session survives restart; the `SessionStore` reads through an
# in-memory hot cache and writes via SQLAlchemy `INSERT` /
# `UPDATE` / `DELETE`.
#
# Float timestamps (`Float`, not `DateTime`): mirror the in-memory
# `RuntimeSession` representation, which uses `time.monotonic()`
# floats. Avoids timezone math at every read; the cleanup query
# `WHERE last_seen_at < cutoff` is a numeric compare.
#
# `last_seen_at` indexed: powers the cleanup_idle sweep.
# `workflow_id` indexed: powers list_sessions / list_for_user.
# `status` indexed: future "all `waiting_confirmation`" lookups.
# ─────────────────────────────────────────────────────────────────
class RuntimeSessionRow(Base):
    __tablename__ = "runtime_sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        String, nullable=False, index=True
    )
    user_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )
    input: Mapped[str] = mapped_column(Text, nullable=False)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="running", server_default="running", index=True
    )  # running | waiting_confirmation | completed | error
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    pending_requirements: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    node_types: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    history: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    started_at: Mapped[float] = mapped_column(Float, nullable=False)
    last_seen_at: Mapped[float] = mapped_column(
        Float, nullable=False, index=True
    )
    workflow_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )