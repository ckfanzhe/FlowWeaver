"""Source-formatting helpers for the compile package.

Before the single-engine refactor  this package was a
standalone "Workflow → Python source" generator with its own
multi-pass pipeline, its own emitter registry, and its own
`GeneratorError`. The `core/compile/` package now owns both the
runtime (build agno objects) and the export (render the same graph
to Python source) — there's only one pipeline left.

What survived here:
  - `utils.py`           — small idents / quoting / docstring helpers.
  - `models.py`          — `model_expr(...)` constructor expressions.
  - `http_wrappers.py`   — HTTP wrapper function definitions.
  - `tools_expr.py`      — `tools=[...]` list expressions.
  - `mcp_lookup.py`      — best-effort MCP server lookup.
  - `imports.py`         — collect agno imports.
  - `assembly.py`        — `Workflow(steps=[...])` block.
  - `ir_helpers.py`      — IR → nodes_by_id view + target_ref helper.

These are leaf utilities consumed by `app.core.strategies.*`
(each strategy's `to_source()` calls them inline) and by
`app.core.compile.serialize`. Keeping them under the `generator.`
namespace (instead of renaming to `app.core.export`) avoids touching
every compile file; the name is historical, the role is "source
helpers".
"""
from __future__ import annotations

__all__: list[str] = []