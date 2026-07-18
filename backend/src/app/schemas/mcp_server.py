"""Pydantic schemas for MCP server CRUD."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

McpTransport = Literal["stdio", "sse"]

# Same camel/snake dual-input config as `LlmPreset` (the Settings
# drawer sends camelCase; cURL / tests pass snake_case; we accept
# both so the API doesn't 422 on whichever the caller chooses).
_CAMEL_INPUT = ConfigDict(populate_by_name=True, extra="ignore")

class McpServerBase(BaseModel):
    model_config = _CAMEL_INPUT

    name: str = Field(min_length=1, max_length=200)
    transport: McpTransport
    enabled: bool = True
    # stdio
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    # sse
    url: str | None = None
    headers: dict[str, str] | None = None

    @model_validator(mode="after")
    def _check_transport_specific(self):
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("stdio transport requires `command`")
        elif self.transport == "sse":
            if not self.url:
                raise ValueError("sse transport requires `url`")
        return self

class McpServerCreate(McpServerBase):
    id: str | None = None  # auto-generated if omitted

class McpServerUpdate(BaseModel):
    model_config = _CAMEL_INPUT

    name: str | None = None
    enabled: bool | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None

class McpServerRead(McpServerBase):
    # Per-user binding, mirrors `LlmPreset.userId`. NULL → system row
    # (shared, read-only); non-NULL → owning user's id. Frontend uses
    # this to disable delete / edit affordances for rows the caller
    # doesn't own.
    userId: Optional[str] = Field(default=None, alias="user_id")
    id: str
    createdAt: datetime
    updatedAt: datetime

    # `populate_by_name=True` lets the response serialise by the
    # Python field name (`userId`, `createdAt`) rather than the
    # snake_case alias — matching the existing `name` / `command`
    # fields and the camelCase contract the frontend expects. The
    # earlier `Config: from_attributes = True` style lacked
    # `populate_by_name`, so FastAPI's `by_alias=True` default emitted
    # `user_id` (snake_case) and the test couldn't find it.
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    @classmethod
    def from_orm_row(cls, row) -> "McpServerRead":
        return cls(
            id=row.id,
            name=row.name,
            transport=row.transport,
            enabled=row.enabled,
            command=row.command,
            args=row.args,
            env=row.env,
            url=row.url,
            headers=row.headers,
            user_id=getattr(row, "user_id", None),
            createdAt=row.created_at,
            updatedAt=row.updated_at,
        )