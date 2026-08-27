"""Pydantic schemas for LLM presets."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Provider = Literal["openai", "anthropic", "ollama", "google"]
PROVIDERS: tuple[str, ...] = ("openai", "anthropic", "ollama", "google")

# Common config: accept both snake_case AND camelCase on input, but
# keep snake_case Python attribute names so the rest of the codebase
# (handlers, tests, db lookups) doesn't need to change. This fixes
# the 422 Unprocessable Entity the Settings drawer hit when saving a
# preset — the frontend sends `{ modelId, apiKey, baseUrl, isDefault }`
# (camelCase) while the schema only declared the snake_case names.
_CAMEL_INPUT = ConfigDict(populate_by_name=True, extra="ignore")

class LlmPresetCreate(BaseModel):
    model_config = _CAMEL_INPUT

    name: str = Field(min_length=1, max_length=200)
    provider: Provider
    model_id: str = Field(
        min_length=1,
        max_length=200,
        alias="modelId",
    )
    api_key: Optional[str] = Field(default=None, alias="apiKey")
    base_url: Optional[str] = Field(default=None, alias="baseUrl")
    is_default: bool = Field(default=False, alias="isDefault")
    # P3 : per-preset "thinking mode" toggle. When True,
    # the runtime applies the provider-specific reasoning kwargs
    # (Claude `thinking={"type": "enabled", ...}`, OpenAI
    # `reasoning_effort="medium"`, Gemini `thinking_budget=N`).
    # Default False — tests stay fast and the toggle is opt-in per
    # preset instead of a global preference.
    thinking: bool = Field(default=False, alias="thinking")
    # Sampling / length knobs. NULL = "don't pass this kwarg to the
    # model" (let the provider default kick in). Ranges match what
    # OpenAI / Anthropic document; out-of-range values 422 at the API
    # before they ever reach the DB.
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0, alias="topP")
    max_tokens: Optional[int] = Field(default=None, ge=1, le=100000, alias="maxTokens")

class LlmPresetUpdate(BaseModel):
    model_config = _CAMEL_INPUT

    name: Optional[str] = None
    provider: Optional[Provider] = None
    model_id: Optional[str] = Field(default=None, alias="modelId")
    api_key: Optional[str] = Field(default=None, alias="apiKey")
    base_url: Optional[str] = Field(default=None, alias="baseUrl")
    is_default: Optional[bool] = Field(default=None, alias="isDefault")
    thinking: Optional[bool] = Field(default=None, alias="thinking")
    # Sampling / length knobs — same semantics as `LlmPresetCreate`.
    # `exclude_unset=True` in the service layer distinguishes "field
    # absent from payload" (leave column alone) from "field present,
    # value None" (clear it back to NULL).
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0, alias="topP")
    max_tokens: Optional[int] = Field(default=None, ge=1, le=100000, alias="maxTokens")

class LlmPresetRead(BaseModel):
    # `populate_by_name=True` lets `from_orm_row` keep passing the
    # snake_case ORM column names (`model_id=row.model_id`, etc.) while
    # ALSO accepting the camelCase names — useful for tests and any
    # Python caller that prefers camelCase. Serialization defaults to
    # the Python field name, so the JSON output is camelCase
    # (`modelId`, `hasApiKey`, `baseUrl`, `isDefault`) — which matches
    # the frontend's TypeScript `LlmPreset` interface exactly.
    #
    # P3 : the previous shape emitted snake_case
    # (`model_id`, `is_default`, `has_api_key`, `base_url`) while
    # `createdAt`/`updatedAt` were already camelCase. That mismatch
    # silently broke two UI features: (1) the LlmTab star button read
    # `p.isDefault` which was always undefined, so default rows showed
    # ☆ instead of ★; (2) the PresetForm read `initial.modelId` etc.
    # which were all undefined, so most fields opened empty even though
    # the API had the data. Both bugs vanished once the API response
    # used camelCase uniformly.
    model_config = ConfigDict(populate_by_name=True)

    id: str
    name: str
    provider: str
    modelId: str = Field(alias="model_id")
    # api_key is intentionally NOT returned to the client after creation
    # to avoid leaking secrets into the browser. We expose a `hasApiKey`
    # boolean so the UI can show whether one is set.
    hasApiKey: bool = Field(alias="has_api_key")
    baseUrl: Optional[str] = Field(default=None, alias="base_url")
    isDefault: bool = Field(alias="is_default")
    # Per-preset thinking flag, surfaced for the Settings → LLM editor
    # so the user can flip it on a single preset without affecting any
    # other preset in the table.
    thinking: bool
    # Sampling / length knobs. Same NULL = "model default" semantics as
    # the create schema; the frontend renders them as empty inputs when
    # the value is NULL so the user knows the model will fall back to
    # its own default rather than seeing a stale `0`.
    temperature: Optional[float] = Field(default=None)
    topP: Optional[float] = Field(default=None, alias="top_p")
    maxTokens: Optional[int] = Field(default=None, alias="max_tokens")
    # Per-user binding. NULL → system row (shared, read-only);
    # non-NULL → owning user's id (matches `users.id`, which for human
    # users is the email). The frontend can disable delete / edit
    # affordances when `userId` differs from the signed-in user.
    userId: Optional[str] = Field(default=None, alias="user_id")
    createdAt: datetime
    updatedAt: datetime

    @classmethod
    def from_orm_row(cls, row) -> "LlmPresetRead":
        # `populate_by_name=True` (see above) accepts BOTH the
        # camelCase Python field name (`modelId=row.model_id` would
        # fail) AND the snake_case alias (`model_id=row.model_id`).
        # We pass by alias here so the ORM-row translation reads
        # naturally — one-to-one mapping from `row.X` to ORM column.
        return cls(
            id=row.id,
            name=row.name,
            provider=row.provider,
            model_id=row.model_id,
            has_api_key=bool(row.api_key),
            base_url=row.base_url,
            is_default=bool(row.is_default),
            thinking=bool(getattr(row, "thinking", False)),
            temperature=getattr(row, "temperature", None),
            top_p=getattr(row, "top_p", None),
            max_tokens=getattr(row, "max_tokens", None),
            user_id=getattr(row, "user_id", None),
            createdAt=row.created_at,
            updatedAt=row.updated_at,
        )
