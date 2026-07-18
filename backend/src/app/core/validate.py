"""Cross-cutting validation helpers used by chat-builder and beyond.

row L : `known_node_type` is the kernel previously
duplicated as `_validate_node_type` (chat_builder_patterns) and
`_check_node_type` (chat_builder_service). Both callers now wrap the
kernel — the chat-builder service turns the `ValueError` into a
`ToolCallRejected` so the LLM gets the structured rejection envelope;
the patterns module lets `ValueError` propagate as-is for back-compat.
"""

from __future__ import annotations

def known_node_type(node_type: str) -> None:
    """Raise `ValueError` if `node_type` is not in the manifest registry.

    The registry lives in `app.core.node_types.NODE_TYPES` and is the
    same source the runtime + canvas use — keeping the chat-builder
    gates in sync prevents the LLM from being told a type is valid
    that the rest of the platform would later reject.
    """
    # Lazy import — `node_types` builds its registry on first access
    # (walks the manifest). Avoid forcing that cost on every chat
    # module import.
    from app.core.node_types import NODE_TYPES

    if node_type in NODE_TYPES:
        return
    # Legacy aliases (`parallel`, `steps`, `router`, `condition`)
    # are accepted here too — the chat-builder pipeline migrates
    # them to the merged type (`flow`, `branch`) before they hit
    # the runtime, so the LLM-facing gate has to pass them through.
    # `app.core._compat.LEGACY_NODE_ALIASES` is the canonical list
    # — see `app/core/_compat.py`.
    from app.core._compat import is_legacy_type
    if is_legacy_type(node_type):
        return
    raise ValueError(
        f"unknown node type {node_type!r}; valid types: "
        f"{sorted(NODE_TYPES)}"
    )

__all__ = ["known_node_type"]