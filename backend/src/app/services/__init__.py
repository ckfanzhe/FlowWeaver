"""Services package — domain / business-logic layer between API endpoints
and the database / executor / generator.

Each service exposes plain Python functions that take a `Session`
explicitly. The API layer is the only place that knows about FastAPI;
the services are framework-agnostic and can be driven from tests, CLI
tools, or background jobs without changes.

Layout:
        workflow_service     create / read / update / delete / list / export
        template_service     list / get / instantiate
        llm_preset_service   CRUD + single-default guarantee
        runtime_service      run / continue / re-run / session inspect

Why a services layer:
   - Today's API endpoints mix HTTP parsing, ORM construction, and
     business rules (e.g. "templates are read-only",
     "`is_default` is a singleton", "empty workflows can't run"). That
     makes them hard to read AND hard to unit-test.
   - Moving the rules into services lets tests drive the business
     logic directly with a `Session` instead of round-tripping through
     FastAPI's TestClient.
"""
from __future__ import annotations

from . import (
    chat_builder_service,
    llm_preset_service,
    runtime_service,
    template_service,
    workflow_service,
)

__all__ = [
    "workflow_service",
    "template_service",
    "llm_preset_service",
    "runtime_service",
    "chat_builder_service",
]