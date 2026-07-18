/**
 * Tests for the canvas edge mapper.
 *
 * Run:  npx tsx --test src/components/Canvas/WorkflowCanvas.test.ts
 *
 * Background: the chat builder's LLM sometimes passes
 * `source_handle='default'` to `connect_nodes` (a common convention in
 * graph-DSL docs). React Flow looks up edges by handle id — when the
 * edge says `sourceHandle='default'` but BaseNode has no handle with
 * that id, React Flow silently drops the edge. The user sees "nodes
 * aren't connected" even though the DB has the edges.
 *
 * The fix has two layers:
 *   1. `connect_nodes` normalizes `'default'` to `None` on the way in
 *      (covered by `test_chat_builder.py::_normalize_chat_handle`).
 *   2. `flowEdge` normalizes `'default'` to `undefined` on render
 *      (this file). The render-time normalizer is a defensive backstop
 *      so that historical DB rows with the bad handle don't render as
 *      invisible edges.
 *
 * These tests pin both helpers so a future refactor can't regress
 * either side.
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import { normalizeHandle, mapFlowEdge } from './edgeMapper'
import type { WorkflowEdge } from '../../types/workflow'

// ───────────────────────────────────────────────────────────────
// normalizeHandle — direct unit tests
// ───────────────────────────────────────────────────────────────

test('normalizeHandle: undefined stays undefined', () => {
  assert.equal(normalizeHandle(undefined), undefined)
})

test('normalizeHandle: null stays undefined', () => {
  assert.equal(normalizeHandle(null as unknown as undefined), undefined)
})

test('normalizeHandle: empty string becomes undefined', () => {
  assert.equal(normalizeHandle(''), undefined)
})

test('normalizeHandle: "default" is collapsed to undefined', () => {
  // The main regression case — the LLM emits this in chat-built
  // edges and BaseNode cannot match it.
  assert.equal(normalizeHandle('default'), undefined)
})

test('normalizeHandle: a real named handle is collapsed to undefined', () => {
  // As of  we strip EVERY non-empty handle id, because
  // our BaseNode has only unnamed handles and the LLM routinely
  // invents ids that don't exist on the node. The chat LLM
  // therefore cannot pass a meaningful handle id today; if/when
  // we add named handles (router branches, tool-source outlets),
  // this test should be revisited.
  assert.equal(normalizeHandle('branch-news'), undefined)
})

test('normalizeHandle: whitespace-only is collapsed to undefined', () => {
  // Whitespace-only is treated as empty.
  assert.equal(normalizeHandle(' '), undefined)
})

test('normalizeHandle: ANY non-empty value is collapsed', () => {
  // The LLM has been seen passing 'default', 'br1', 'br2', 'input',
  // 'branch-news', etc. — all of these silently break the edge
  // because BaseNode has no matching handle. Treat them all as
  // "no handle" so the edge renders on the default unnamed handle.
  for (const bad of ['default', 'br1', 'br2', 'input', 'branch-news', 'x']) {
    assert.equal(normalizeHandle(bad), undefined, `expected '${bad}' to collapse`)
  }
})

// ───────────────────────────────────────────────────────────────
// mapFlowEdge — the full React Flow shape
// ───────────────────────────────────────────────────────────────

const baseEdge: WorkflowEdge = {
  id: 'edge-1',
  source: 'ro',
  target: 'we',
}

test('mapFlowEdge: edge without handles is rendered with no handle ids', () => {
  const out = mapFlowEdge(baseEdge)
  assert.equal(out.id, 'edge-1')
  assert.equal(out.source, 'ro')
  assert.equal(out.target, 'we')
  assert.equal(out.sourceHandle, undefined)
  assert.equal(out.targetHandle, undefined)
  assert.equal(out.type, 'dataflow')
})

test('mapFlowEdge: LLM-built "default" handles are stripped to undefined', () => {
  // The actual case from the user's chat: the LLM passed
  // source_handle='default' / target_handle='default' and the edge
  // disappeared. After the fix, the mapper drops both to undefined so
  // React Flow uses the node's default source/target positions.
  const out = mapFlowEdge({
    ...baseEdge,
    sourceHandle: 'default',
    targetHandle: 'default',
  })
  assert.equal(out.sourceHandle, undefined)
  assert.equal(out.targetHandle, undefined)
  // Source and target IDs are untouched.
  assert.equal(out.source, 'ro')
  assert.equal(out.target, 'we')
})

test('mapFlowEdge: a real branch handle is also collapsed', () => {
  // As of  we strip EVERY non-empty handle id, because
  // our BaseNode has only unnamed handles. If/when we add named
  // router-branch handles, this test should change to assert the
  // branch id survives.
  const out = mapFlowEdge({
    ...baseEdge,
    sourceHandle: 'branch-news',
    targetHandle: undefined,
  })
  assert.equal(out.sourceHandle, undefined)
  assert.equal(out.targetHandle, undefined)
})

test('mapFlowEdge: any handle id on either side is stripped', () => {
  // Mixed case — both source and target have non-empty ids; both
  // collapse to undefined so the edge renders on the default
  // unnamed handles.
  const out = mapFlowEdge({
    ...baseEdge,
    sourceHandle: 'br1',
    targetHandle: 'input',
  })
  assert.equal(out.sourceHandle, undefined)
  assert.equal(out.targetHandle, undefined)
  // Source and target node IDs are untouched.
  assert.equal(out.source, 'ro')
  assert.equal(out.target, 'we')
})

test('mapFlowEdge: historical "br1"/"input" bad rows are fixed at render time', () => {
  // The actual DB row on the user's workflow `wf-d7c8c18c`:
  //   edge sourceHandle="br1" targetHandle="input"
  // After this fix, the mapper drops both to undefined so React
  // Flow routes the edge through the single default handle.
  const out = mapFlowEdge({
    ...baseEdge,
    sourceHandle: 'br1',
    targetHandle: 'input',
  })
  assert.deepEqual(
    { sourceHandle: out.sourceHandle, targetHandle: out.targetHandle },
    { sourceHandle: undefined, targetHandle: undefined },
  )
})

test('mapFlowEdge: tool_attachment kinds map to the tool_attachment edge type', () => {
  const out = mapFlowEdge({ ...baseEdge, kind: 'tool_attachment' })
  assert.equal(out.type, 'tool_attachment')
})

test('mapFlowEdge: dataflow kind is the default edge type', () => {
  // Both absent and explicit 'dataflow' should resolve to the same
  // React Flow edge type — the canvas's default edge renderer is
  // DataflowEdge.
  assert.equal(mapFlowEdge({ ...baseEdge }).type, 'dataflow')
  assert.equal(mapFlowEdge({ ...baseEdge, kind: 'dataflow' }).type, 'dataflow')
})
