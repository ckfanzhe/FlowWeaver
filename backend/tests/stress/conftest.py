"""Stress suite conftest — re-exports the existing per-test fixtures.

The shared `seeded_default_preset` fixture (defined in
`tests/conftest.py`) sets up an in-memory SQLite + a default Claude
LLM preset + a deterministic `[label] echo: input` agent stub. Stress
tests inherit the same setup so agent nodes in complex workflows emit
predictable text — that lets us assert on the exact echo payload.

We don't redefine the fixture here; pytest auto-discovers
`tests/conftest.py` first, then the closer `tests/stress/conftest.py`,
and the latter can `usefixtures`/`inherit` the former via the
`pytest_collection_modifyitems` hook chain. The simplest approach is
to leave this file empty and let pytest find `seeded_default_preset`
via the standard parent-conftest inheritance.
"""
from __future__ import annotations

# Intentionally empty: parent `tests/conftest.py` provides all the
# fixtures we need (`seeded_default_preset`, `real_llm_preset`,
# `engine`, `db`, `client`). Stress tests just declare them in their
# function signatures and pytest wires the rest up via the standard
# conftest inheritance chain.