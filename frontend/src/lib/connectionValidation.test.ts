/**
 * Regression tests for the per-edge-kind connection validator.
 *
 * Run:  npx tsx --test src/lib/connectionValidation.test.ts
 *
 * : the validator gained a second rule table
 * (`tool_attachment`) that is consulted for edges with
 * `kind="tool_attachment"`. These tests pin the new behaviour in TS so
 * the frontend can't drift from the Python validator. The Python
 * equivalents live in `backend/tests/test_connection_rules.py`.
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import type { WorkflowEdge, WorkflowNode } from '../types/workflow'

import {
  TOOL_ATTACHMENT_RULES,
  CONNECTION_RULES,
  TOOL_SOURCE_TYPES,
  edgeKindOf,
  validateConnections,
  wouldBeValidConnection,
} from './connectionValidation'

// ─────────────────────────────────────────────────────────────────
// Tiny builders — match the Python `_n` / `_e` helpers in style.
// ─────────────────────────────────────────────────────────────────
function n(id: string, type: string, config: Record<string, unknown> = {}): WorkflowNode {
  return {
    id,
    type,
    position: { x: 0, y: 0 },
    data: { label: id, config },
  }
}
function e(src: string, tgt: string, kind: 'dataflow' | 'tool_attachment' | undefined = undefined): WorkflowEdge {
  return {
    id: `e-${src}-${tgt}`,
    source: src,
    target: tgt,
    ...(kind ? { kind } : {}),
  }
}

// ─────────────────────────────────────────────────────────────────
// Sanity: both tables loaded from the shared JSON
// ─────────────────────────────────────────────────────────────────
test('CONNECTION_RULES and TOOL_ATTACHMENT_RULES both load from shared JSON', () => {
  // Every node type has a row in both tables (the table may be empty
  // rules per-type, but a row exists so `rules[t]` is non-null).
  // : the three tool-source types
  // (http/mcp/tools) collapsed into a single 'tool' node.
  for (const t of ['agent', 'tool']) {
    assert.ok(CONNECTION_RULES[t], `dataflow table missing ${t}`)
    assert.ok(TOOL_ATTACHMENT_RULES[t], `tool_attachment table missing ${t}`)
  }
})

test('edgeKindOf normalises unknown to dataflow', () => {
  assert.equal(edgeKindOf({}), 'dataflow')
  assert.equal(edgeKindOf({ kind: undefined }), 'dataflow')
  assert.equal(edgeKindOf({ kind: '' }), 'dataflow')
  assert.equal(edgeKindOf({ kind: 'dataflow' }), 'dataflow')
  assert.equal(edgeKindOf({ kind: 'tool_attachment' }), 'tool_attachment')
  assert.equal(edgeKindOf({ kind: 'something_weird' }), 'dataflow')
})

// ─────────────────────────────────────────────────────────────────
//  — wikipedia / tavily_search / duckduckgo /
// calculator / arxiv_search collapsed into the unified `tool` node's
// `preset` config discriminator. They no longer have their own
// `NodeType` literal in the generated union, so the prior
// wikipedia-specific validator tests are removed. The `tool` type
// is the single tool_source entry — connection rules apply
// uniformly to all 3 `source` modes (`mcp` / `http` / `function`)
// AND to all 5 collapsed `preset` values.
//
// The phase  regression test ("wikipedia is a
// tool_source") is replaced by a broader "tool is a tool_source"
// sanity check below — the same invariant survives the collapse
// (any preset toolkit that's been migrated via `_compat` to
// `type='tool'` must validate cleanly as a tool_source).
// ─────────────────────────────────────────────────────────────────
test('tool is a tool_source type (covers all 5 collapsed presets via cfg.preset)', () => {
  // : the 5 preset tool types (wikipedia /
  // tavily_search / duckduckgo / calculator / arxiv_search) all
  // collapse into `tool` + `preset='<name>'`. The connection
  // validator only inspects the type literal (always `tool`) so
  // preset vs. plain http/mcp/function source is invisible to it —
  // one entry covers the whole surface.
  assert.ok(
    TOOL_SOURCE_TYPES.has('tool'),
    '`tool` (which now also covers the 5 collapsed presets via cfg.preset) must be in TOOL_SOURCE_TYPES',
  )
})

// ─────────────────────────────────────────────────────────────────
// validateConnections — per-kind dispatch
// ─────────────────────────────────────────────────────────────────
test('validateConnections accepts tool→agent via tool_attachment edge', () => {
  const nodes = [n('t', 'tool'), n('a', 'agent')]
  const edges = [e('t', 'a', 'tool_attachment')]
  assert.deepEqual(validateConnections(nodes, edges), [])
})

test('validateConnections accepts tool→agent via tool_attachment edge for each source', () => {
  // : the three tool-source types
  // (http/mcp/tools) collapsed into a single 'tool' node. The
  // validator only inspects the type literal (which is always
  // 'tool'), so this test no longer needs to parametrize — any
  // single invocation exercises the full surface.
  const nodes = [n('s', 'tool'), n('a', 'agent')]
  const edges = [e('s', 'a', 'tool_attachment')]
  assert.deepEqual(validateConnections(nodes, edges), [])
})

test('validateConnections rejects agent→tools even with kind=tool_attachment', () => {
  const nodes = [n('a', 'agent'), n('t', 'tool')]
  const edges = [e('a', 't', 'tool_attachment')]
  const errs = validateConnections(nodes, edges)
  assert.ok(errs.some((e) => e.code === 'incompatibleSource'))
})

test('validateConnections rejects tools→tools even with kind=tool_attachment', () => {
  const nodes = [n('t1', 'tool'), n('t2', 'tool')]
  const edges = [e('t1', 't2', 'tool_attachment')]
  const errs = validateConnections(nodes, edges)
  assert.ok(errs.some((e) => e.code === 'incompatibleSource'))
})

test('validateConnections rejects tools→agent under default (dataflow) kind', () => {
  const nodes = [n('t', 'tool'), n('a', 'agent')]
  const edges = [e('t', 'a')] // kind=None → dataflow
  const errs = validateConnections(nodes, edges)
  assert.ok(
    errs.some((e) => e.code === 'incompatibleSource'),
    'dataflow tool → agent must be rejected',
  )
})

test('validateConnections accepts one tool wired to many agents', () => {
  const nodes = [
    n('t', 'tool'),
    n('a1', 'agent'),
    n('a2', 'agent'),
    n('a3', 'agent'),
  ]
  const edges = [
    e('t', 'a1', 'tool_attachment'),
    e('t', 'a2', 'tool_attachment'),
    e('t', 'a3', 'tool_attachment'),
  ]
  assert.deepEqual(validateConnections(nodes, edges), [])
})

//  — REMOVED: `validateConnections still flags
// incomplete branch (noThen) when only tool edges exist`.
// Post-N2, `branch`'s connection rule has `min_outgoing=0` per design —
// the strict `if-else` min=1 check lives at the strategy layer
// (`BranchStrategy._build_if_else`), not at the connection validator.
// The old `condition` type's `min_outgoing=1` rule was migrated to
// the strategy layer when N2 collapsed `condition`+`router` into
// `branch`. Connection-validation-level `noThen` is no longer a
// meaningful code path for branch.

test('validateConnections accepts dataflow + tool_attachment coexisting', () => {
  const nodes = [
    n('a1', 'agent'),
    n('a2', 'agent'),
    n('t', 'tool'),
  ]
  const edges = [
    e('a1', 'a2'),                              // dataflow
    e('t', 'a1', 'tool_attachment'),            // tool wiring
    e('t', 'a2', 'tool_attachment'),            // tool wiring
  ]
  assert.deepEqual(validateConnections(nodes, edges), [])
})

// ─────────────────────────────────────────────────────────────────
// wouldBeValidConnection — drag-time validator
// ─────────────────────────────────────────────────────────────────
test('wouldBeValidConnection accepts tool→agent when kind=tool_attachment', () => {
  const nodes = [n('t', 'tool'), n('a', 'agent')]
  assert.deepEqual(
    wouldBeValidConnection('t', 'a', nodes, [], 'tool_attachment'),
    [],
  )
})

test('wouldBeValidConnection rejects tool→agent under default (dataflow) kind', () => {
  const nodes = [n('t', 'tool'), n('a', 'agent')]
  const errs = wouldBeValidConnection('t', 'a', nodes, [], 'dataflow')
  assert.ok(errs.some((e) => e.code === 'incompatibleSource'))
})

test('wouldBeValidConnection rejects agent→tools under tool_attachment kind', () => {
  const nodes = [n('a', 'agent'), n('t', 'tool')]
  const errs = wouldBeValidConnection('a', 't', nodes, [], 'tool_attachment')
  assert.ok(errs.some((e) => e.code === 'incompatibleSource'))
})

test('wouldBeValidConnection allows same (src, tgt) pair under different kinds', () => {
  // The (a1, a2) dataflow edge already exists. Adding a tool_attachment
  // for the same pair would be a duplicate — but only within the same
  // kind. Different kinds are independent.
  const nodes = [n('a1', 'agent'), n('a2', 'agent'), n('t', 'tool')]
  const edges = [e('a1', 'a2')] // dataflow
  // Now ask: would adding `t → a2` as tool_attachment be valid?
  assert.deepEqual(
    wouldBeValidConnection('t', 'a2', nodes, edges, 'tool_attachment'),
    [],
  )
})

test('wouldBeValidConnection rejects duplicate same-kind edge', () => {
  const nodes = [n('t', 'tool'), n('a', 'agent')]
  const edges = [e('t', 'a', 'tool_attachment')]
  const errs = wouldBeValidConnection('t', 'a', nodes, edges, 'tool_attachment')
  assert.ok(errs.some((e) => e.code === 'duplicateEdge'))
})

test('wouldBeValidConnection selfLoop works under either kind', () => {
  const nodes = [n('t', 'tool')]
  for (const kind of ['dataflow', 'tool_attachment'] as const) {
    const errs = wouldBeValidConnection('t', 't', nodes, [], kind)
    assert.ok(errs.some((e) => e.code === 'selfLoop'), `kind=${kind}`)
  }
})