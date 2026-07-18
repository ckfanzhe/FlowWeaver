/**
 * Unit tests for the pure canvas node helpers in `canvasNodes.ts`.
 *
 * Run:  npx tsx --test src/components/Canvas/canvasNodes.test.ts
 *
 * Background:
 *   The canvas maintains a local, React-Flow-controlled copy of the
 *   store's nodes (`rfNodes`) so React Flow can animate position
 *   changes during drag without round-tripping through Zustand on
 *   every mousemove. The store is only updated when the drag ends
 *   (`onNodeDragStop`). Whenever the store changes — a config form
 *   keystroke, a structural mutation, an import — we have to
 *   re-seed `rfNodes` from the store, BUT preserve the position of
 *   any node the user is currently dragging, otherwise an in-flight
 *   drag would snap back to the start point the moment the user
 *   types into a property panel on a different node.
 *
 *   `resyncRfNodes(prevRfNodes, storeNodes, preserveIds)` encodes
 *   that contract. The `preserveIds` set is what the canvas tracks
 *   via React Flow's `onNodeDragStart` / `onNodeDragStop` callbacks.
 *   These tests pin all four behaviours:
 *     1. no drag in progress → take everything from the store
 *     2. drag in progress     → preserve local position for that id
 *     3. unrelated edits      → flow through the store normally
 *     4. paste / layout       → store position wins for non-dragged ids
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import type { Edge, Node } from '@xyflow/react'
import { flowEdge, flowNode, resyncRfEdges, resyncRfNodes } from './canvasNodes'
import type { WorkflowEdge, WorkflowNode } from '../../types/workflow'

const NONE = new Set<string>()

// ───────────────────────────────────────────────────────────────
// flowNode — domain → React Flow shape
// ───────────────────────────────────────────────────────────────

test('flowNode: maps id, type, position, and data verbatim', () => {
  const wn: WorkflowNode = {
    id: 'a',
    type: 'agent',
    position: { x: 10, y: 20 },
    data: { label: 'Agent A', model: { provider: 'openai' } },
  }
  const out = flowNode(wn)
  assert.equal(out.id, 'a')
  assert.equal(out.type, 'agent')
  assert.deepEqual(out.position, { x: 10, y: 20 })
  assert.deepEqual(out.data, { label: 'Agent A', model: { provider: 'openai' } })
})

// ───────────────────────────────────────────────────────────────
// flowEdge — thin wrapper around `mapFlowEdge`; the heavy lifting
// is already covered by `WorkflowCanvas.test.ts`. Here we just pin
// the wiring so a future refactor doesn't drop the import.
// ───────────────────────────────────────────────────────────────

test('flowEdge: delegates to mapFlowEdge (dataflow default)', () => {
  const we: WorkflowEdge = { id: 'e1', source: 'a', target: 'b' }
  const out = flowEdge(we)
  assert.equal(out.id, 'e1')
  assert.equal(out.source, 'a')
  assert.equal(out.target, 'b')
  assert.equal(out.type, 'dataflow')
})

test('flowEdge: tool_attachment kind is preserved', () => {
  const out = flowEdge({ id: 'e2', source: 'a', target: 'b', kind: 'tool_attachment' })
  assert.equal(out.type, 'tool_attachment')
})

// ───────────────────────────────────────────────────────────────
// resyncRfNodes — the live-drag contract
// ───────────────────────────────────────────────────────────────

const nodeA: WorkflowNode = {
  id: 'a',
  type: 'agent',
  position: { x: 0, y: 0 },
  data: { label: 'A' },
}
const nodeB: WorkflowNode = {
  id: 'b',
  type: 'router',
  position: { x: 100, y: 100 },
  data: { label: 'B' },
}

test('resyncRfNodes: empty previous + no preserve → take everything from the store', () => {
  // Initial mount: no in-flight state, no drags in progress. Just
  // seed from the store.
  const out = resyncRfNodes([], [nodeA, nodeB], NONE)
  assert.deepEqual(
    out.map((n) => n.id),
    ['a', 'b'],
  )
  assert.deepEqual(out[0].position, { x: 0, y: 0 })
  assert.deepEqual(out[1].position, { x: 100, y: 100 })
})

test('resyncRfNodes: a node whose position the user is dragging is preserved', () => {
  // The R7 regression. React Flow has been mutating `nodeA.position`
  // locally during a drag (e.g. x=250, y=180) but the store still
  // reports x=0, y=0. The canvas marked `a` as in-flight via
  // onNodeDragStart. After the fix, the local position wins.
  const prev: Node[] = [
    { id: 'a', type: 'agent', position: { x: 250, y: 180 }, data: { label: 'A' } },
    { id: 'b', type: 'router', position: { x: 100, y: 100 }, data: { label: 'B' } },
  ]
  const out = resyncRfNodes(prev, [nodeA, nodeB], new Set(['a']))
  const a = out.find((n) => n.id === 'a')!
  assert.deepEqual(a.position, { x: 250, y: 180 }, 'in-flight drag position must win')
})

test('resyncRfNodes: a non-dragged node takes the store position', () => {
  // The user is dragging `a`. `b` has never moved — its store
  // position is the truth; the local copy's stale position must NOT
  // win. This is the import / layout / paste case in disguise:
  // anything that legitimately repositions a node via the store
  // (paste drops it at a new spot, layout algorithm moves it, etc.)
  // depends on the store winning for ids NOT in `preserveIds`.
  const prev: Node[] = [
    { id: 'a', type: 'agent', position: { x: 999, y: 999 }, data: { label: 'A' } },
    { id: 'b', type: 'router', position: { x: 0, y: 0 }, data: { label: 'B' } },
  ]
  const out = resyncRfNodes(prev, [nodeA, nodeB], new Set(['a']))
  const b = out.find((n) => n.id === 'b')!
  assert.deepEqual(b.position, { x: 100, y: 100 }, 'untouched node tracks store, not stale prev')
})

test('resyncRfNodes: a freshly-added node (no entry in prev) takes the store position', () => {
  // The user is dragging `a`. A new node `c` arrives from the store
  // (palette drop). `c` is not in `preserveIds` (it isn't being
  // dragged) and not in `prev` (it's new) — it falls through to the
  // store position naturally.
  const nodeC: WorkflowNode = {
    id: 'c',
    // : http + mcp + tools → tool
    type: 'tool',
    position: { x: 50, y: 50 },
    data: { toolName: 'http_call' },
  }
  const prev: Node[] = [
    { id: 'a', type: 'agent', position: { x: 999, y: 999 }, data: { label: 'A' } },
  ]
  const out = resyncRfNodes(prev, [nodeA, nodeC], new Set(['a']))
  const a = out.find((n) => n.id === 'a')!
  const c = out.find((n) => n.id === 'c')!
  assert.deepEqual(a.position, { x: 999, y: 999 }, 'a drag is preserved')
  assert.deepEqual(c.position, { x: 50, y: 50 }, 'c starts at store position')
})

test('resyncRfNodes: data changes flow through from the store (with preserve)', () => {
  // The store just received a config-form update for `a` (its
  // `label` changed). The user is also dragging `a` from x=0 to
  // x=300. After the reseed, both the new label AND the local
  // drag position must be visible — data comes from the store,
  // position comes from the local copy for ids in `preserveIds`.
  const updatedA: WorkflowNode = {
    ...nodeA,
    data: { label: 'A — new label' },
  }
  const prev: Node[] = [
    { id: 'a', type: 'agent', position: { x: 300, y: 0 }, data: { label: 'A' } },
  ]
  const out = resyncRfNodes(prev, [updatedA], new Set(['a']))
  const a = out[0]
  assert.deepEqual(a.position, { x: 300, y: 0 }, 'drag position preserved')
  assert.deepEqual(a.data, { label: 'A — new label' }, 'new data flows through')
})

test('resyncRfNodes: data changes flow through from the store (no preserve)', () => {
  // Same as above but the user isn't dragging anything. The store
  // update must flow through completely — position too.
  const updatedA: WorkflowNode = {
    ...nodeA,
    data: { label: 'A — new label' },
    position: { x: 0, y: 0 },
  }
  const prev: Node[] = [
    { id: 'a', type: 'agent', position: { x: 0, y: 0 }, data: { label: 'A' } },
  ]
  const out = resyncRfNodes(prev, [updatedA], NONE)
  assert.deepEqual(out[0].data, { label: 'A — new label' })
  assert.deepEqual(out[0].position, { x: 0, y: 0 })
})

test('resyncRfNodes: removing a node from the store drops it from the local copy', () => {
  // The user deleted `b`. The store no longer has `b`. The reseed
  // must drop it too, otherwise it would stick around on the canvas.
  const prev: Node[] = [
    { id: 'a', type: 'agent', position: { x: 0, y: 0 }, data: { label: 'A' } },
    { id: 'b', type: 'router', position: { x: 100, y: 100 }, data: { label: 'B' } },
  ]
  const out = resyncRfNodes(prev, [nodeA], NONE)
  assert.equal(out.length, 1)
  assert.equal(out[0].id, 'a')
})

test('resyncRfNodes: a committed drag (store position === local position) is stable', () => {
  // After `onNodeDragStop`, the store position equals the local
  // position and the id is no longer in `preserveIds`. The reseed
  // must not produce a different node for the same id — i.e. the
  // position preservation is idempotent for the post-commit state.
  const a: WorkflowNode = { ...nodeA, position: { x: 50, y: 60 } }
  const prev: Node[] = [
    { id: 'a', type: 'agent', position: { x: 50, y: 60 }, data: { label: 'A' } },
  ]
  const out = resyncRfNodes(prev, [a], NONE)
  const outA = out[0]
  assert.deepEqual(outA.position, { x: 50, y: 60 })
  assert.deepEqual(outA.data, { label: 'A' })
})

test('resyncRfNodes: type changes flow through (no stale type)', () => {
  // The store reports node `a` was retyped from `agent` to
  // `condition`. The local `prev` still says `agent` — that was
  // only true at one point in the past. After the reseed, the type
  // must reflect the store's current truth.
  const retyped: WorkflowNode = { ...nodeA, type: 'condition' }
  const prev: Node[] = [
    { id: 'a', type: 'agent', position: { x: 50, y: 60 }, data: { label: 'A' } },
  ]
  const out = resyncRfNodes(prev, [retyped], NONE)
  assert.equal(out[0].type, 'condition')
})

test('resyncRfNodes: paste / layout repositioning works', () => {
  // Paste or layout-algorithm case: a previously-placed node `b`
  // has been moved by the store to a new position. The local copy
  // is stale. Without anything in `preserveIds`, the store must win.
  // This is the inverse of the drag-preservation case and the reason
  // `preserveIds` is REQUIRED — a "preserve whenever prev differs"
  // rule would break this scenario.
  const movedB: WorkflowNode = {
    id: 'b',
    type: 'router',
    position: { x: 800, y: 600 }, // layout algorithm moved it
    data: { label: 'B' },
  }
  const prev: Node[] = [
    { id: 'b', type: 'router', position: { x: 100, y: 100 }, data: { label: 'B' } },
  ]
  const out = resyncRfNodes(prev, [movedB], NONE)
  assert.deepEqual(out[0].position, { x: 800, y: 600 }, 'store-driven move must win')
})

test('resyncRfNodes: a dragged node is preserved even with multiple ids in preserveIds', () => {
  // Multi-select drag: React Flow can drag several nodes at once.
  // The canvas tracks all of them in `preserveIds`. Every id in
  // the set must get its local position preserved.
  const nodeC: WorkflowNode = {
    id: 'c',
    type: 'flow',
    position: { x: 50, y: 50 },
    data: { label: 'C', config: { mode: 'parallel' } },
  }
  const prev: Node[] = [
    { id: 'a', type: 'agent', position: { x: 700, y: 700 }, data: { label: 'A' } },
    { id: 'c', type: 'flow', position: { x: 555, y: 555 }, data: { label: 'C', config: { mode: 'parallel' } } },
  ]
  const out = resyncRfNodes(prev, [nodeA, nodeC], new Set(['a', 'c']))
  assert.deepEqual(out.find((n) => n.id === 'a')!.position, { x: 700, y: 700 })
  assert.deepEqual(out.find((n) => n.id === 'c')!.position, { x: 555, y: 555 })
})

test('resyncRfNodes: an id in preserveIds but missing from prev falls back to the store', () => {
  // Defensive case: a race where `preserveIds` contains an id that
  // the local copy never had (e.g. a node was just added and a
  // drag-start fired before the local seeded). Should not crash;
  // should fall through to the store position.
  const out = resyncRfNodes([], [nodeA], new Set(['a']))
  assert.deepEqual(out[0].position, { x: 0, y: 0 })
})

/**
 * Background:
 *   resyncRfEdges is the edge half of the rfNodes pattern. Before
 *   the local-copy pattern, `rfEdges` was a pure useMemo over
 *   `edges.map(flowEdge)` so React Flow's local `selected: true`
 *   mutation (set on left-click) was discarded on every render —
 *   no visible highlight, no working left-click-to-select-and-delete
 *   UX. The canvas now owns a local copy, mutated via
 *   `applyEdgeChanges`, and re-seeded from the store on every store
 *   change via this helper. The preservation rule is simple:
 *   `selected: true` survives the re-seed for ids still in the
 *   store.
 */
const edgeAB: WorkflowEdge = { id: 'e1', source: 'a', target: 'b' }
const edgeBC: WorkflowEdge = { id: 'e2', source: 'b', target: 'c' }

test('resyncRfEdges: empty previous + no selection → take everything from the store', () => {
  // Initial mount: no prior local copy, nothing selected. Just seed
  // from the store.
  const out = resyncRfEdges([], [edgeAB, edgeBC])
  assert.deepEqual(
    out.map((e) => e.id),
    ['e1', 'e2'],
  )
  assert.equal(out[0].selected, undefined)
  assert.equal(out[1].selected, undefined)
})

test('resyncRfEdges: a selected edge survives the re-seed', () => {
  // The user left-clicked e1; React Flow set `selected: true` on it
  // in the local copy. The store has since updated (some other edge
  // event fired). The selected flag MUST survive the re-seed, or
  // the highlight vanishes and Delete becomes a no-op.
  const prev: Edge[] = [
    { id: 'e1', source: 'a', target: 'b', type: 'dataflow', selected: true },
    { id: 'e2', source: 'b', target: 'c', type: 'dataflow' },
  ]
  const out = resyncRfEdges(prev, [edgeAB, edgeBC])
  assert.equal(out.find((e) => e.id === 'e1')!.selected, true)
  assert.equal(out.find((e) => e.id === 'e2')!.selected, undefined)
})

test('resyncRfEdges: a freshly-added edge (no entry in prev) is unselected', () => {
  // The user just dropped a new connection. The store has e3; the
  // prev copy doesn't. It must fall through to the store-derived
  // base (no prior selection to preserve) and render unselected.
  const edgeAD: WorkflowEdge = { id: 'e3', source: 'a', target: 'd' }
  const prev: Edge[] = [
    { id: 'e1', source: 'a', target: 'b', type: 'dataflow', selected: true },
  ]
  const out = resyncRfEdges(prev, [edgeAB, edgeAD])
  const e3 = out.find((e) => e.id === 'e3')!
  assert.equal(e3.selected, undefined, 'new edge starts unselected')
  // The previously-selected e1 must still be selected.
  assert.equal(out.find((e) => e.id === 'e1')!.selected, true)
})

test('resyncRfEdges: removing an edge from the store drops it from the local copy', () => {
  // The store deleted e2. The reseed must drop it too, otherwise
  // it would stick around on the canvas as a zombie.
  const prev: Edge[] = [
    { id: 'e1', source: 'a', target: 'b', type: 'dataflow', selected: true },
    { id: 'e2', source: 'b', target: 'c', type: 'dataflow' },
  ]
  const out = resyncRfEdges(prev, [edgeAB])
  assert.equal(out.length, 1)
  assert.equal(out[0].id, 'e1')
  // e1's selection survives even though the store reference changed.
  assert.equal(out[0].selected, true)
})

test('resyncRfEdges: a previously-selected edge that the store has now de-selected gets cleared', () => {
  // The store actively cleared the selection (e.g. the user
  // left-clicked the pane, which fires `{type: 'select', selected:
  // false}` for every selected edge). The next render's local copy
  // must reflect the cleared state — otherwise stale selection
  // persists across renders.
  //
  // This is encoded by the helper itself: it only preserves
  // `selected: true` from prev. If prev doesn't carry `selected:
  // true` for an id (because the most-recent render already
  // reflected the cleared state), the re-seed just uses the
  // store-derived base, which has no `selected` field.
  const prev: Edge[] = [
    { id: 'e1', source: 'a', target: 'b', type: 'dataflow' }, // already cleared
  ]
  const out = resyncRfEdges(prev, [edgeAB])
  assert.equal(out[0].selected, undefined)
})

test('resyncRfEdges: tool_attachment kind is preserved through the re-seed', () => {
  // Edge kind is part of the React Flow type discriminator; the
  // resync must not drop it on the way through. (Regression net
  // for the bug where the helper shadowed the kind.)
  const ta: WorkflowEdge = { id: 'e1', source: 't', target: 'a', kind: 'tool_attachment' }
  const prev: Edge[] = [
    { id: 'e1', source: 't', target: 'a', type: 'tool_attachment', selected: true },
  ]
  const out = resyncRfEdges(prev, [ta])
  assert.equal(out[0].type, 'tool_attachment')
  assert.equal(out[0].selected, true)
})