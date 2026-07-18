/**
 * Pure helpers for mapping the workflow store's `WorkflowNode` /
 * `WorkflowEdge` domain objects to React Flow's `Node` / `Edge`
 * shape, and for re-syncing a local React-Flow-controlled copy of
 * those objects when the store changes.
 *
 * `flowNode` / `flowEdge` are trivial mappers — no behaviour, just
 * shape.
 *
 * `resyncRfNodes` is the interesting one for nodes: it takes the
 * previous React Flow nodes (which React Flow has been mutating
 * locally — at minimum, the dragged node's position) and the new
 * store nodes, and produces the next local copy. Position is
 * preserved ONLY for ids currently in `preserveIds` (i.e. nodes the
 * user is actively dragging); everything else (data, type, position
 * for non-dragged nodes, selection, edges, dimensions — anything
 * the store or a non-drag React Flow change might have updated) is
 * taken from the store.
 *
 * Why `preserveIds` is explicit:
 *   During a drag React Flow mutates `rfNodes[i].position` in place
 *   via `applyNodeChanges`, but the store's `position` field stays
 *   stale until `onNodeDragStop` commits it. If we naively reseed
 *   `rfNodes = store.nodes.map(flowNode)` whenever the store updates,
 *   a concurrent edit on a different node (e.g. typing into a config
 *   form fires `updateNodeData` and triggers the store's `nodes`
 *   reference to change) would clobber the in-flight drag's position
 *   back to the start point. The user sees the dragged node snap to
 *   its origin instead of following the cursor. But we cannot use the
 *   simple heuristic "if `prev` had a different position than the
 *   store, keep prev" — that rule would also break legitimate
 *   store-driven moves (paste, layout algorithm, import), which
 *   look identical to a drag from a position-comparison standpoint.
 *   Tracking the ACTIVE drag ids via React Flow's drag-start/stop
 *   callbacks is the unambiguous signal.
 *
 * `resyncRfEdges` is the edge analogue — it preserves React Flow's
 * local `selected` flag for edges that still exist in the store, so
 * a click-to-select followed by Delete still works even if the store
 * updates between the two events. Edges have no drag-position concern,
 * so the function is much simpler than `resyncRfNodes` (no
 * `preserveIds` set needed).
 *
 * The functions are pure so they can be unit-tested without React
 * Flow or Zustand — see `canvasNodes.test.ts`.
 */
import type { Edge, Node } from '@xyflow/react'
import type { WorkflowEdge, WorkflowNode } from '../../types/workflow'
import { mapFlowEdge } from './edgeMapper'

export function flowNode(n: WorkflowNode): Node {
  return {
    id: n.id,
    type: n.type,
    position: n.position,
    data: n.data,
  }
}

export function flowEdge(e: WorkflowEdge): Edge {
  return mapFlowEdge(e)
}

export function resyncRfNodes(
  prevRfNodes: Node[],
  storeNodes: WorkflowNode[],
  preserveIds: ReadonlySet<string>,
): Node[] {
  // Index previous nodes by id so the lookup is O(1). We only consult
  // this for ids in `preserveIds` (currently-dragging nodes) — every
  // other node takes its position from the store, so paste/import/
  // layout moves don't get clobbered.
  const prevById = new Map(prevRfNodes.map((n) => [n.id, n]))
  return storeNodes.map((n) => {
    const base = flowNode(n)
    if (preserveIds.has(n.id)) {
      const local = prevById.get(n.id)
      if (local) return { ...base, position: local.position }
    }
    return base
  })
}

/**
 * Re-seed `rfEdges` from the store, preserving the `selected` flag
 * React Flow has locally mutated for any edge still in the store.
 *
 * map(flowEdge)`. That meant React Flow's local
 *   `selected: true` mutation (set when the user left-clicks an
 *   edge) was lost on every render — the visual highlight never
 *   appeared, and pressing Delete with an edge "selected" did
 *   nothing because the render tree had no selection. The canvas
 *   had no working left-click-to-select-and-delete UX for edges.
 *   This helper is the edge half of the `rfNodes` pattern: the
 *   local copy is owned by the canvas, mutated via `applyEdgeChanges`
 *   on React Flow events, and re-seeded here when the store changes
 *   — but selection survives the re-seed because the store doesn't
 *   know about it and the prev copy does.
 *
 * Edges have no drag-position concern, so this is much simpler than
 * `resyncRfNodes` — no `preserveIds` set, just a `selected`-only
 * preservation rule keyed on id membership.
 */
export function resyncRfEdges(
  prevRfEdges: Edge[],
  storeEdges: WorkflowEdge[],
): Edge[] {
  // Index previous edges by id, but only keep the ones React Flow
  // marked as selected. Edges not in the prev copy (just added by the
  // store) fall through to the store-derived base; edges that React
  // Flow didn't select don't need any preservation.
  const prevSelected = new Map<string, Edge>()
  for (const e of prevRfEdges) {
    if (e.selected) prevSelected.set(e.id, e)
  }
  return storeEdges.map((storeEdge) => {
    const base = flowEdge(storeEdge)
    const local = prevSelected.get(storeEdge.id)
    if (local) return { ...base, selected: true }
    return base
  })
}