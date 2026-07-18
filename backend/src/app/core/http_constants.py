"""Shared HTTP status constants — one place to update when standards shift.

Starlette deprecated `HTTP_422_UNPROCESSABLE_ENTITY` in favour of
`HTTP_422_UNPROCESSABLE_CONTENT` (per RFC 9110). Newer releases still ship
the old name; future ones won't. Resolve once at import time.
"""
from __future__ import annotations

from fastapi import status

# Pick the modern name if available, fall back to the legacy one. Both
# return 422 — only the Python constant name differs.
#
# NOTE: We probe with `hasattr` rather than nested `getattr` because
# `getattr(status, "<deprecated>", default)` still triggers the
# deprecation warning at import time (the attribute access happens, the
# default is just ignored). `hasattr` is a pure existence check and
# is warning-free.
if hasattr(status, "HTTP_422_UNPROCESSABLE_CONTENT"):
    HTTP_422: int = status.HTTP_422_UNPROCESSABLE_CONTENT
elif hasattr(status, "HTTP_422_UNPROCESSABLE_ENTITY"):
    HTTP_422 = status.HTTP_422_UNPROCESSABLE_ENTITY
else:
    HTTP_422 = 422

