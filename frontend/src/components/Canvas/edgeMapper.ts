/**
 * Edge mapper helpers — pure functions shared between the canvas
 * and the regression tests.
 *
 * Lives in its own module so the tests can import the helpers
 * without dragging in the canvas component (which pulls in
 * @xyflow/react's CSS at module load and trips Node's
 * ERR_UNKNOWN_FILE_EXTENSION under tsx).
 */
import type { Edge } from '@xyflow/react'
import type { WorkflowEdge } from '../../types/workflow'

/**
 * Collapse a chat-LLM-emitted handle value to the React Flow canonical
 * "no handle" form.
 *
 * The LLM reliably invents handle ids that don't exist on our nodes —
 * the most common offender is the literal string `'default'` (a
 * convention in graph-DSL docs), but the model has also been observed
 * passing `'br1'`, `'br2'`, `'input'`, etc. for router / agent nodes
 * that only expose a single unnamed handle. BaseNode has exactly one
 * source handle and one target handle, both unnamed — so ANY non-empty
 * handle id will fail to match React Flow's handle lookup and the edge
 * silently renders invisible.
 *
 * The frontend mapper is a defensive backstop that handles historical
 * DB rows; the backend's `_normalize_chat_handle` is the primary fix.
 * Both collapse every non-empty value to `undefined` so the edge lands
 * on the single default handle and renders.
 *
 * Exported for `WorkflowCanvas.test.ts`.
 */
export function normalizeHandle(h: string | undefined | null): string | undefined {
  if (!h) return undefined
  if (!h.trim()) return undefined
  return undefined
}

/**
 * Map a domain `WorkflowEdge` to React Flow's `Edge`. The render-time
 * normalizer is a defensive backstop so that historical DB rows with
 * the bad handle don't render as invisible edges.
 */
export function mapFlowEdge(e: WorkflowEdge): Edge {
  return {
    id: e.id,
    source: e.source,
    target: e.target,
    sourceHandle: normalizeHandle(e.sourceHandle),
    targetHandle: normalizeHandle(e.targetHandle),
    type: e.kind === 'tool_attachment' ? 'tool_attachment' : 'dataflow',
  }
}
