/**
 * Trace store — per-node execution timeline + live status map.
 *
 * `entries` is an append-only list of nodes that have STARTED executing.
 * The matching `node_end` event fills in `durationMs` / `tokens` /
 * `status`. The canvas reads `statuses[nodeId]` to know whether to draw
 * a running/done/error dot on each node; the trace panel reads `entries`
 * to render the timeline.
 */
import { create } from 'zustand'
import type { RuntimeEvent, NodeType } from '../types/workflow'

export type NodeStatus = 'idle' | 'running' | 'ok' | 'error'

export interface TraceEntry {
  /** Workflow node id (NOT the React Flow node id — they're the same here). */
  nodeId: string
  nodeType: NodeType
  label: string
  /** ms since session start, from the backend's monotonic clock. */
  startedAt: number
  /** Filled in when the matching node_end arrives. */
  endedAt: number | null
  durationMs: number | null
  status: NodeStatus
  error: string | null
  tokens: { input: number; output: number; total: number } | null
}

interface State {
  /** True when at least one node is mid-execution. */
  running: boolean
  /** Per-node status, indexed by nodeId. Drives the canvas dots. */
  statuses: Record<string, NodeStatus>
  /** The timeline entries, in execution order. */
  entries: TraceEntry[]
  /** Total tokens consumed by LLM-backed nodes in the current run. */
  totalTokens: { input: number; output: number; total: number }
  /** Re-run-from support: the input the current run was started with. */
  lastInput: string
}

interface Actions {
  /** Reset all trace state. Called when a new run begins. */
  reset: (input?: string) => void
  /** Feed an SSE event into the store. Unknown events are ignored. */
  apply: (ev: RuntimeEvent) => void
}

export const useTraceStore = create<State & Actions>((set) => ({
  running: false,
  statuses: {},
  entries: [],
  totalTokens: { input: 0, output: 0, total: 0 },
  lastInput: '',

  reset: (input = '') =>
    set({
      running: false,
      statuses: {},
      entries: [],
      totalTokens: { input: 0, output: 0, total: 0 },
      lastInput: input,
    }),

  apply: (ev) => {
    if (ev.type === 'node_start') {
      set((s) => {
        // If we're seeing node_start events, the run is live.
        const statuses = { ...s.statuses, [ev.nodeId]: 'running' as NodeStatus }
        const entries: TraceEntry[] = [
          ...s.entries,
          {
            nodeId: ev.nodeId,
            nodeType: ev.nodeType,
            label: ev.label,
            startedAt: ev.t,
            endedAt: null,
            durationMs: null,
            status: 'running',
            error: null,
            tokens: null,
          },
        ]
        return { running: true, statuses, entries }
      })
      return
    }
    if (ev.type === 'node_end') {
      set((s) => {
        const statuses = { ...s.statuses, [ev.nodeId]: ev.status }
        const entries = s.entries.map((e) =>
          e.nodeId === ev.nodeId && e.endedAt === null
            ? {
                ...e,
                endedAt: ev.t,
                durationMs: ev.durationMs,
                status: ev.status,
                error: ev.error ?? null,
                tokens: ev.tokens ?? null,
              }
            : e,
        )
        let totalTokens = s.totalTokens
        if (ev.tokens) {
          totalTokens = {
            input: s.totalTokens.input + (ev.tokens.input ?? 0),
            output: s.totalTokens.output + (ev.tokens.output ?? 0),
            total: s.totalTokens.total + (ev.tokens.total ?? 0),
          }
        }
        return { statuses, entries, totalTokens }
      })
      return
    }
    // `confirmation` (ask / tool_confirm) means the workflow is
    // PAUSED — the active node will not emit `node_end` until the user
    // replies. Mark the run as not-in-flight so the chat panel stops
    // showing "Waiting" (it's now waiting for the user, not the engine).
    if (ev.type === 'confirmation') {
      set({ running: false })
      return
    }
    if (ev.type === 'completed' || ev.type === 'error') {
      set({ running: false })
    }
  },
}))