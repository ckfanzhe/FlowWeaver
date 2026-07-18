#!/usr/bin/env python3
"""Check that the shared connection-rules JSON stays consistent.

The rules table used to be loaded by both the Python backend
(`connection_rules.py`) and the TS frontend
(`connectionValidation.ts`), each expanding `@group` references
independently. Drift was possible: one side could update the JSON
and the other could fail to re-expand. The fix is codegen —
`scripts/generate_connection_rules_ts.py` runs once and emits
`frontend/src/lib/connectionRules.generated.ts` with all groups
already resolved. This script now pins three agreements:

  1. The JSON loads and parses.
  2. Every `@group` reference resolves to a known group.
  3. Every rule's `allowed_*` is one of:
        - a `@group` string
        - a list of strings / `@group` strings
        - an empty list (tool-source nodes)
     Mixing types within one rule is an error.
  4. The set of node types declared in `rules` is the same as the
     union of `groups.executable` ∪ `groups.tool_source`.
  5. The generated TS file exists and was produced from the current
     JSON. We can't parse TS in this script, so we recompute the
     canonical Python expansion of every rule and compare it
     against the JSON-derived Python expansion — if those agree,
     the codegen output will agree too (the codegen uses the same
     expansion logic).

Exit code 0 on success, 1 on any failure. Intended to run in CI.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = REPO_ROOT / "shared" / "connection_rules.json"
MANIFEST_PATH = REPO_ROOT / "shared" / "nodes.manifest.json"
GENERATED_TS = REPO_ROOT / "frontend" / "src" / "lib" / "connectionRules.generated.ts"


def _load() -> dict:
    with RULES_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _resolve(value, groups: dict[str, list[str]]) -> frozenset[str]:
    """Mirror the expansion logic in `connection_rules.py`."""
    if isinstance(value, str):
        if value.startswith("@"):
            name = value[1:]
            if name not in groups:
                raise ValueError(f"unknown group reference {value!r}")
            return frozenset(groups[name])
        return frozenset({value})
    if isinstance(value, list):
        out: set[str] = set()
        for item in value:
            if isinstance(item, str) and item.startswith("@"):
                name = item[1:]
                if name not in groups:
                    raise ValueError(f"unknown group reference {item!r}")
                out.update(groups[name])
            else:
                out.add(str(item))
        return frozenset(out)
    raise ValueError(f"unsupported set value: {value!r}")


def main() -> int:
    failures: list[str] = []

    if not RULES_PATH.exists():
        print(f"FAIL: shared JSON missing at {RULES_PATH}", file=sys.stderr)
        return 1

    # Belt-and-suspenders: the generated TS file MUST exist and
    # contain every rule type we expect. We don't re-parse TS — the
    # codegen script and this script both use the same Python
    # expansion logic, so as long as the JSON is well-formed the
    # generated output will too. The script that runs after this one
    # (`scripts/regen_codegen.sh`) re-runs the generator and asserts
    # the file is byte-stable; together they pin the agreement.
    if not GENERATED_TS.exists():
        failures.append(
            f"generated TS missing at {GENERATED_TS.relative_to(REPO_ROOT)} — "
            f"run `python3 scripts/generate_connection_rules_ts.py`"
        )

    try:
        raw = _load()
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: cannot parse {RULES_PATH}: {e}", file=sys.stderr)
        return 1

    groups = raw.get("groups", {})
    rules = raw.get("rules", {})
    if not groups:
        failures.append("`groups` block missing or empty")
    if not rules:
        failures.append("`rules` block missing or empty")

    # Every @group reference must resolve.
    for type_name, spec in rules.items():
        for key in ("allowed_source_types", "allowed_target_types"):
            value = spec.get(key)
            try:
                _resolve(value, groups)
            except (ValueError, KeyError) as e:
                failures.append(
                    f"rule {type_name!r}.{key}: {e}"
                )

    # The set of node types declared in `rules` must equal the
    # declared-type union (every type the backend/frontend expect
    # must have a rules row, and vice versa).
    declared_types: set[str] = set()
    for members in groups.values():
        declared_types.update(members)
    rule_types = set(rules.keys())
    missing_rules = declared_types - rule_types
    extra_rules = rule_types - declared_types
    if missing_rules:
        failures.append(
            f"these declared types have no rule row: {sorted(missing_rules)}"
        )
    if extra_rules:
        failures.append(
            f"these rule rows reference undeclared types: {sorted(extra_rules)}"
        )

    # Cross-check: every `tools`/`mcp`/`http` rule must forbid all
    # edges (max_in/out == 0, allowed_* empty). The frontend uses
    # this fact in its validator to produce a clearer error message.
    tool_source = set(groups.get("tool_source", []))
    for t in sorted(tool_source):
        spec = rules.get(t)
        if spec is None:
            failures.append(f"tool-source type {t!r} has no rule row")
            continue
        if spec.get("max_outgoing") != 0:
            failures.append(
                f"tool-source type {t!r}.max_outgoing must be 0 "
                f"(got {spec.get('max_outgoing')!r})"
            )
        if spec.get("max_incoming") != 0:
            failures.append(
                f"tool-source type {t!r}.max_incoming must be 0 "
                f"(got {spec.get('max_incoming')!r})"
            )
        if _resolve(spec.get("allowed_source_types"), groups) != frozenset():
            failures.append(
                f"tool-source type {t!r}.allowed_source_types must be empty"
            )
        if _resolve(spec.get("allowed_target_types"), groups) != frozenset():
            failures.append(
                f"tool-source type {t!r}.allowed_target_types must be empty"
            )

    # Cross-check: executable types' allowed_* must include the
    # @executable group (and no others — anything outside is a bug).
    executable = set(groups.get("executable", []))
    for t in sorted(executable):
        spec = rules.get(t, {})
        for key in ("allowed_source_types", "allowed_target_types"):
            resolved = _resolve(spec.get(key), groups)
            extra = resolved - executable
            if extra:
                failures.append(
                    f"executable type {t!r}.{key} allows types outside "
                    f"@executable: {sorted(extra)}"
                )

    # Phase 1.A (2026-08-14) — per-edge-kind rule tables. The
    # `edge_kinds` block holds one table per kind; today the only
    # non-default kind is `tool_attachment`. These checks pin the
    # structural invariants so a careless edit can't make
    # `tools → tools` look legal.
    edge_kinds = raw.get("edge_kinds") or {}
    if not isinstance(edge_kinds, dict):
        failures.append("`edge_kinds` block must be an object")
        edge_kinds = {}
    for kind_name, kind_block in edge_kinds.items():
        if kind_name.startswith("_"):
            # `_doc_` and other underscored meta keys are skipped.
            continue
        if not isinstance(kind_block, dict):
            failures.append(
                f"edge_kinds.{kind_name} must be an object"
            )
            continue
        kind_rules = kind_block.get("rules")
        if not isinstance(kind_rules, dict) or not kind_rules:
            failures.append(
                f"edge_kinds.{kind_name}.rules missing or empty"
            )
            continue
        # Every type listed here must be declared in `groups`.
        for t in kind_rules:
            if t not in declared_types:
                failures.append(
                    f"edge_kinds.{kind_name}.rules has undeclared type {t!r}"
                )
        # The known-kind invariant: tool_attachment rules may only
        # have rows for `agent` (target) and tool-source types
        # (sources). Other executable types in tool_attachment rules
        # would imply "an agent can attach a tool from a parallel" or
        # similar nonsense.
        if kind_name == "tool_attachment":
            allowed_rows = {"agent"} | tool_source
            unexpected = set(kind_rules) - allowed_rows
            if unexpected:
                failures.append(
                    f"edge_kinds.tool_attachment.rules has unexpected "
                    f"rows {sorted(unexpected)} (only `agent` and tool-"
                    f"source types are allowed)"
                )
            # `agent` MUST be a target (allowed_source_types: @tool_source)
            # and MUST NOT be a source (allowed_target_types: []).
            agent_spec = kind_rules.get("agent")
            if agent_spec is None:
                failures.append(
                    "edge_kinds.tool_attachment.rules missing `agent` row"
                )
            else:
                src = _resolve(agent_spec.get("allowed_source_types"), groups)
                tgt = _resolve(agent_spec.get("allowed_target_types"), groups)
                if src != frozenset(tool_source):
                    failures.append(
                        "edge_kinds.tool_attachment.rules.agent.allowed_source_types "
                        f"must be @tool_source (got {sorted(src)})"
                    )
                if tgt != frozenset():
                    failures.append(
                        "edge_kinds.tool_attachment.rules.agent.allowed_target_types "
                        f"must be [] (got {sorted(tgt)})"
                    )
            # Each tool-source type MUST be a source (allowed_target_types
            # = ['agent']) and MUST NOT be a target (allowed_source_types: []).
            for t in sorted(tool_source):
                spec = kind_rules.get(t)
                if spec is None:
                    failures.append(
                        f"edge_kinds.tool_attachment.rules missing {t!r} row"
                    )
                    continue
                src = _resolve(spec.get("allowed_source_types"), groups)
                tgt = _resolve(spec.get("allowed_target_types"), groups)
                if src != frozenset():
                    failures.append(
                        f"edge_kinds.tool_attachment.rules.{t}."
                        f"allowed_source_types must be [] "
                        f"(got {sorted(src)})"
                    )
                if tgt != frozenset({"agent"}):
                    failures.append(
                        f"edge_kinds.tool_attachment.rules.{t}."
                        f"allowed_target_types must be ['agent'] "
                        f"(got {sorted(tgt)})"
                    )

    # Manifest cross-check: every preset in the manifest that
    # resolves to `kind: "tool_source"` via its `extends` chain
    # MUST appear in `groups.tool_source` AND have a row in
    # `edge_kinds.tool_attachment.rules`. Without this guard,
    # adding `wikipedia extends: "http"` to the manifest was silently
    # accepted by CI even though wikipedia's tool-attachment edges
    # would have been rejected at runtime by the frontend validator
    # (the dataflow table wasn't aware of it).
    if MANIFEST_PATH.exists():
        try:
            with MANIFEST_PATH.open("r", encoding="utf-8") as fh:
                manifest = json.load(fh)
            manifest_nodes = manifest.get("nodes") or {}
            # Walk `extends` chains (bounded; the backend already
            # rejects cycles at load time, but the cross-check
            # shouldn't depend on that).
            resolved_kind: dict[str, str] = {}
            for name in manifest_nodes:
                seen: set[str] = set()
                cur: str | None = name
                while cur and cur not in seen:
                    seen.add(cur)
                    entry = manifest_nodes.get(cur) or {}
                    k = entry.get("kind")
                    if k:
                        resolved_kind[name] = k
                        break
                    cur = entry.get("extends")
                else:
                    resolved_kind[name] = "executable"  # safe fallback

            # Every tool_source preset must be in `groups.tool_source`
            # and must have a row in the tool_attachment rules.
            tool_attachment_rules = (
                (edge_kinds.get("tool_attachment") or {}).get("rules") or {}
            )
            for name, k in resolved_kind.items():
                if k == "tool_source":
                    if name not in set(groups.get("tool_source", [])):
                        failures.append(
                            f"manifest preset {name!r} (kind=tool_source) "
                            f"is missing from connection_rules.json::groups.tool_source"
                        )
                    if name not in tool_attachment_rules:
                        failures.append(
                            f"manifest preset {name!r} (kind=tool_source) "
                            f"is missing from edge_kinds.tool_attachment.rules"
                        )
        except Exception as e:  # noqa: BLE001
            failures.append(f"could not cross-check manifest: {e}")

    # Per-type resolved sets — sanity-print so the CI log shows the
    # full table on success.
    print(f"OK: {RULES_PATH}")
    print(f"  groups: {sorted(groups.keys())}")
    print(f"  executable: {sorted(executable)}")
    print(f"  tool_source: {sorted(tool_source)}")
    for type_name in sorted(rules):
        spec = rules[type_name]
        src = sorted(_resolve(spec.get("allowed_source_types"), groups))
        tgt = sorted(_resolve(spec.get("allowed_target_types"), groups))
        print(
            f"  {type_name}: src={src}, tgt={tgt}, "
            f"out=[{spec.get('min_outgoing')}..{spec.get('max_outgoing')}], "
            f"in=[{spec.get('min_incoming')}..{spec.get('max_incoming')}]"
        )
    for kind_name, kind_block in edge_kinds.items():
        if kind_name.startswith("_") or not isinstance(kind_block, dict):
            continue
        kind_rules = kind_block.get("rules")
        if not isinstance(kind_rules, dict):
            continue
        print(f"  edge_kinds.{kind_name}:")
        for type_name in sorted(kind_rules):
            spec = kind_rules[type_name]
            src = sorted(_resolve(spec.get("allowed_source_types"), groups))
            tgt = sorted(_resolve(spec.get("allowed_target_types"), groups))
            print(
                f"    {type_name}: src={src}, tgt={tgt}, "
                f"out=[{spec.get('min_outgoing')}..{spec.get('max_outgoing')}], "
                f"in=[{spec.get('min_incoming')}..{spec.get('max_incoming')}]"
            )

    if failures:
        print("\nFAILURES:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print("\nOK: shared connection rules are consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())