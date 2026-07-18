"""Pydantic schemas for the user identity endpoints — .

Two endpoints:
  POST /api/v1/users/identify  — upsert the caller by email
  GET  /api/v1/users/me        — fetch the caller (from X-User-Id header)

Email is the user identifier. The frontend prompt asks for an email on
first visit, stores it in localStorage, and forwards it on every
request as `X-User-Id`. On a new device the user types the same
email — `/users/identify` looks up the existing row (or creates a
fresh one if the email is unknown) and the frontend re-hydrates the
workflow list.

The  follow-up adds `language`, `avatarId`, and `theme` so
the identity row doubles as the user's preference record. The
frontend reads `language` / `theme` on every boot and seeds the
client state from them before the first render — no need to refresh
after the user picks a different theme / language on another
device.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

class IdentifyRequest(BaseModel):
    """Body for `POST /api/v1/users/identify`.

    `email` is normalised to lowercase server-side. The response
    includes `created` so the frontend can distinguish a brand-new
    user from a returning one without a second round-trip.

    `language`, `avatarId`, and `theme` are optional. When supplied,
    they overwrite the stored preference; when omitted, the existing
    value is kept. The frontend always re-asserts the current locale
    + theme on identify so returning users on a new device pick up
    the right state immediately.
    """
    email: str = Field(min_length=3, max_length=320)
    language: str | None = Field(default=None, max_length=8)
    avatarId: str | None = Field(default=None, max_length=32)
    theme: str | None = Field(default=None, max_length=8)

class IdentifyResult(BaseModel):
    """Response for `POST /api/v1/users/identify`."""
    userId: str
    email: str
    tenantId: str
    created: bool
    createdAt: datetime
    language: str | None = None
    avatarId: str | None = None
    theme: str | None = None

class MeResult(BaseModel):
    """Response for `GET /api/v1/users/me`.

    `userId` mirrors the `X-User-Id` header value the caller sent.
    `email` is NULL for the `user-default` placeholder row (the
    anonymous back-compat path).
    """
    userId: str
    email: str | None
    tenantId: str
    language: str | None = None
    avatarId: str | None = None
    theme: str | None = None

__all__ = ["IdentifyRequest", "IdentifyResult", "MeResult"]