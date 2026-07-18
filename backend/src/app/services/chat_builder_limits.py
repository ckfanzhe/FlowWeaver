"""Chat-builder tunable limits — single source of truth.

row I : extracted from `chat_builder_service.py`
where the three constants used to live interleaved with tool
implementations. Future work — moving these into runtime settings
so the user can dial them per-workflow — now has one place to wire.

NB: the constants are re-exported from `chat_builder_service` for
backward compat with the existing `cbs.MAX_TOOL_CALLS_PER_TURN`
test references (and the few in-module call sites that read them
through the module namespace). New code should import from HERE.
"""

from __future__ import annotations

# Per-turn imperative-call cap. The LLM is steered toward
# `plan_workflow` (one call → many nodes/edges) over chaining many
# `add_node`/`update_node` calls. 40 buys space when the LLM ignores
# that guidance or when the chat iterates on small adjustments after
# a plan.
#
# NB: this cap is module-level, not configurable from the UI yet —
# changing it requires a backend redeploy. A follow-up will move it
# into runtime settings (the same place LLM temperature / model
# picker live) so the user can dial it per-workflow.
MAX_TOOL_CALLS_PER_TURN = 40

# Per-session hard cap on pending changes. Defends against the LLM
# looping `update_node` forever — every chat turn has its own cap,
# but we also bound the cumulative diff applied by a single session.
MAX_PENDING_CHANGES_PER_SESSION = 32

# F6  — per-turn rejection budget. After
# `REJECTION_BUDGET_PER_TURN` consecutive rejections the LLM is
# almost certainly looping (calling the same mutating tool with
# slight variations that all fail). The escalation hint tells it
# to pause and call a diagnostic instead. We do NOT hard-cap —
# the user is in charge; the hint is just a nudge.
REJECTION_BUDGET_PER_TURN = 5

__all__ = [
    "MAX_TOOL_CALLS_PER_TURN",
    "MAX_PENDING_CHANGES_PER_SESSION",
    "REJECTION_BUDGET_PER_TURN",
]