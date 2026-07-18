"""SQLAlchemy engine + session factory + FastAPI dependency."""
from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

# `check_same_thread=False` is required for SQLite to work with FastAPI's threadpool.
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
    echo=False,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db() -> None:
    """Create all tables + run idempotent light migrations. Safe on every startup.

    `create_all()` only adds new tables, not new columns to existing ones,
    so we also probe a few `PRAGMA table_info` columns and `ALTER TABLE` if
    they're missing. This is the project's "migrations" layer — no Alembic
    to keep the demo self-contained.
    """
    # Import models so they register on the metadata before create_all.
    from app.db import models  # noqa: F401
    from app.db.base import Base

    # Ensure SQLite file parent exists.
    if engine.url.database and engine.url.database != ":memory:":
        Path(engine.url.database).parent.mkdir(parents=True, exist_ok=True)

    Base.metadata.create_all(bind=engine)

    # In-place light migrations: add columns that are missing on the
    # existing tables. Each migration is a no-op if the column already
    # exists, so re-running is safe.
    _add_column_if_missing("workflows", "is_template", "BOOLEAN NOT NULL DEFAULT 0")
    _add_column_if_missing("workflows", "category", "VARCHAR")
    # Locale tag. Mirrors the JSON's `locale` field
    # so the API can filter the gallery by language. `create_all` only
    # adds new tables, not new columns, so we probe + ALTER here so
    # dev databases that pre-date the column still boot.
    _add_column_if_missing(
        "workflows", "locale", "VARCHAR NOT NULL DEFAULT 'en'"
    )
    # Per-preset "thinking mode" toggle. New column on `llm_presets`;
    # the ALTER TABLE adds it to existing dev databases so the app
    # keeps booting without a manual schema reset.
    _add_column_if_missing("llm_presets", "thinking", "BOOLEAN NOT NULL DEFAULT 0")
    # Multi-user / collaboration. New `created_by` + `tenant_id` columns
    # on `workflows` so list / permission lookups can scope by user
    # without a join. Default to the placeholder values (`user-default`
    # / `tenant-default`) so the migration is backward-compatible with
    # all existing rows.
    _add_column_if_missing(
        "workflows", "created_by", "VARCHAR NOT NULL DEFAULT 'user-default'"
    )
    _add_column_if_missing(
        "workflows", "tenant_id", "VARCHAR NOT NULL DEFAULT 'tenant-default'"
    )
    # Per-user language preference and avatar choice. Nullable
    # so the user can keep using the app without picking either;
    # the frontend treats NULL as "no preference stored, fall back
    # to localStorage / browser default".
    _add_column_if_missing("users", "language", "VARCHAR(8)")
    _add_column_if_missing("users", "avatar_id", "VARCHAR(32)")
    # Per-user theme preference. Same nullability rationale as
    # `language` / `avatar_id`: NULL means "no preference stored
    # yet" so the frontend can fall back to localStorage / browser
    # default.
    _add_column_if_missing("users", "theme", "VARCHAR(8)")
    # Per-user LLM / MCP config binding: rows with
    # `user_id = NULL` are system-shared and read-only via the API;
    # rows with `user_id = <users.id>` belong to a single user and
    # only that user can mutate them. Nullable on purpose so the
    # pre-binding rows (and any platform-seeded "starter" rows) keep
    # working without backfill.
    _add_column_if_missing("llm_presets", "user_id", "VARCHAR")
    _add_column_if_missing("mcp_servers", "user_id", "VARCHAR")

def _add_column_if_missing(table: str, column: str, ddl_type: str) -> None:
    """Add a column to `table` if it doesn't already exist.

    SQLite raises `OperationalError` on duplicate columns only at INSERT
    time, so we proactively check via `PRAGMA table_info` first.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if column in {c["name"] for c in inspector.get_columns(table)}:
        return
    with engine.begin() as conn:
        conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {ddl_type}'))

class _SessionScope:
    """Context manager for ad-hoc DB work outside of FastAPI request scope.

    Usage:
        with session_scope() as db:
            db.add(...)
    Commits on success, rolls back on exception, always closes.
    """
    def __enter__(self) -> Session:
        self._db = SessionLocal()
        return self._db

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self._db.commit()
            else:
                self._db.rollback()
        finally:
            self._db.close()

def session_scope() -> _SessionScope:
    return _SessionScope()