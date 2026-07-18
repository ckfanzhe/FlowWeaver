"""Single-engine compile pipeline.

Public API:
  - `build_workflow(...)` — turn `(nodes, edges)` into an agno
    `Workflow` object. The runtime calls this, then `Wf.run(...)`.
  - `to_python_source(...)` — turn the same `Workflow` into a Python
    source file for export. Reads the same emitters; the source
    mirrors the runtime behaviour byte-for-byte.

Why a single pipeline for both paths?
--------------------------------------
Before this module the runtime executor (`core.workflow_builder`) and
the export generator (`core.generator`) each implemented their own
graph traversal. Every node type had TWO implementations and they
drifted — that's the bug class that produced "the runtime picked 
when the user picked ". One pipeline, one pass order, one
outcome.

Deliberately NOT here:
  - agno event translation (that's `app.core.event_adapter`).
  - session persistence (handled by `Wf.session_state`).
  - legacy runtime state (`sess.cursor`, `_last_text`, etc. — gone).
"""
from __future__ import annotations

from .condition import make_evaluator, migrate_legacy_condition, parse_condition_template
from .errors import CompileError
from .pipeline import CompileCtx, build_workflow
from .run import (
    DEFAULT_RUN_TIMEOUT_SEC,
    LegStep,
    continue_leg,
    drive_leg_with_trace,
    extract_node_types,
    run_leg,
)
from .serialize import to_python_source

__all__ = [
    "build_workflow",
    "continue_leg",
    "drive_leg_with_trace",
    "extract_node_types",
    "LegStep",
    "DEFAULT_RUN_TIMEOUT_SEC",
    "run_leg",
    "to_python_source",
    "CompileCtx",
    "CompileError",
    "parse_condition_template",
    "migrate_legacy_condition",
    "make_evaluator",
]
