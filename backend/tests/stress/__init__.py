"""Stress test suite for AgnoBuilder.

These tests target complex workflow scenarios that the regular
unit-test suite (`tests/test_*.py`) does NOT cover:

  - Deep nesting: Parallel → Loop → Agent at 3+ levels
  - Long chains: 20+ sequential Agent nodes
  - Parallel × Loop combinations
  - Multi-branch routers (4+ branches)
  - Export round-trip fidelity (export → import → re-export byte-identical)
  - Export edge cases: Unicode / RTL / 50KB+ strings / circular refs
  - Concurrent workflow runs
  - Tool-heavy agents (50+ tools feeding one agent)
  - HITL complete chain coverage (pause + resume + state + nested)
  - Session resume after simulated restart

Each scenario is its own `test_<scenario>.py` file. Helpers live in
`tests/stress/_factory.py` (workflow builders) and `tests/stress/_hits.py`
(HITL-specific helpers).
"""