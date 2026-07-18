"""Compile-time errors raised by the build pipeline.

These are construction-time failures (bad config, missing model, dangling
edge). They get surfaced as the workflow's first `ErrorEvent` so the
SSE stream sees a clean failure instead of a Python traceback.
"""
from __future__ import annotations

class CompileError(Exception):
    """The workflow could not be compiled into an agno `Workflow`."""
