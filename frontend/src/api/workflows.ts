/**
 * Workflow CRUD + Runtime API client.
 * Mirrors backend/src/app/api/workflows.py and runtime.py.
 */
import { api } from './client'
import type {
  TemplateSummary,
  Workflow,
  WorkflowNode,
  WorkflowEdge,
  WorkflowSummary,
  McpServerConfig,
  RuntimeEvent,
} from '../types/workflow'

// ─────────────────────────────────────────────────────────────────
// Workflow CRUD
// ─────────────────────────────────────────────────────────────────
export const workflowsApi = {
  list: (scope: 'user' | 'templates' | 'all' = 'user') =>
    api.get<WorkflowSummary[]>(`/api/v1/workflows?scope=${scope}`),

  get: (id: string) => api.get<Workflow>(`/api/v1/workflows/${id}`),

  /** Lightweight gallery view of built-in templates. */
  listTemplates: () =>
    api.get<TemplateSummary[]>('/api/v1/workflows/templates'),

  /** Clone a built-in template into a fresh user workflow. */
  instantiateTemplate: (id: string) =>
    api.post<Workflow>(`/api/v1/workflows/from-template/${id}`),

  create: (data: {
    name: string
    description?: string
    nodes: WorkflowNode[]
    edges: WorkflowEdge[]
  }) =>
    api.post<Workflow>('/api/v1/workflows', {
      name: data.name,
      description: data.description ?? null,
      nodes: data.nodes,
      edges: data.edges,
    }),

  replace: (id: string, data: {
    name: string
    description?: string | null
    nodes: WorkflowNode[]
    edges: WorkflowEdge[]
  }) =>
    api.put<Workflow>(`/api/v1/workflows/${id}`, {
      name: data.name,
      description: data.description ?? null,
      nodes: data.nodes,
      edges: data.edges,
    }),

  patch: (id: string, data: Partial<{
    name: string
    description: string | null
    nodes: WorkflowNode[]
    edges: WorkflowEdge[]
  }>) =>
    api.patch<Workflow>(`/api/v1/workflows/${id}`, data),

  remove: (id: string) => api.delete(`/api/v1/workflows/${id}`),

  /** Fetch the workflow's rendered Python source. The caller decides whether to
   *  download it, show it in a preview, etc. */
  exportPython: async (id: string, fallbackName: string): Promise<{ code: string; filename: string }> => {
    // Uses `api.fetchRaw` (not raw `fetch`) so the `X-User-Id` header is
    // injected — the export endpoint requires a workflow-member role, and
    // going through plain `fetch` would default the caller to
    // `user-default` and 403 every workflow owned by a real email.
    const res = await api.fetchRaw(`/api/v1/workflows/${id}/export`)
    if (!res.ok) {
      let msg = `HTTP ${res.status}`
      try {
        const body = await res.json()
        msg = body.detail ?? msg
      } catch { /* ignore */ }
      throw new Error(msg)
    }
    const cd = res.headers.get('content-disposition') ?? ''
    const m = /filename="([^"]+)"/.exec(cd)
    const filename = m?.[1] ?? `${fallbackName.replace(/[^a-zA-Z0-9_-]+/g, '_')}.py`
    const code = await res.text()
    return { code, filename }
  },

  /** Download the workflow as a versioned JSON envelope. */
  exportJson: async (id: string, fallbackName: string): Promise<{ envelope: Record<string, unknown>; filename: string; raw: string }> => {
    // Same header-injection rationale as `exportPython` above.
    const res = await api.fetchRaw(`/api/v1/workflows/${id}/export-json`)
    if (!res.ok) {
      let msg = `HTTP ${res.status}`
      try {
        const body = await res.json()
        msg = body.detail ?? msg
      } catch { /* ignore */ }
      throw new Error(msg)
    }
    const cd = res.headers.get('content-disposition') ?? ''
    const m = /filename="([^"]+)"/.exec(cd)
    const filename = m?.[1] ?? `${fallbackName.replace(/[^a-zA-Z0-9_-]+/g, '_')}.json`
    const raw = await res.text()
    const envelope = JSON.parse(raw)
    return { envelope, filename, raw }
  },

  /** Send a JSON envelope to the backend to create a new workflow. */
  importJson: (envelope: unknown) =>
    api.post<Workflow>('/api/v1/workflows/import-json', { payload: envelope }),
}

// ─────────────────────────────────────────────────────────────────
// MCP servers
// ─────────────────────────────────────────────────────────────────
export const mcpServersApi = {
  list: () => api.get<McpServerConfig[]>('/api/v1/mcp-servers'),
  create: (data: Omit<McpServerConfig, 'id'> & { id?: string }) =>
    api.post<McpServerConfig>('/api/v1/mcp-servers', data),
  remove: (id: string) => api.delete(`/api/v1/mcp-servers/${id}`),
}

// ─────────────────────────────────────────────────────────────────
// Runtime — SSE stream consumer
// ─────────────────────────────────────────────────────────────────
export interface RunHandle {
  sessionId: string | null
  events: RuntimeEvent[]
}

export async function runWorkflowStream(
  workflowId: string,
  input: string,
  onEvent: (ev: RuntimeEvent) => void,
  sessionId?: string,
  signal?: AbortSignal,
): Promise<string> {
  //  regression: use `api.fetchRaw` (NOT raw `fetch`) so the
  // `X-User-Id` header is injected from localStorage. Without the
  // header the backend defaults to `user-default`, which has no
  // `WorkflowMember` row for the caller's workflow → 403 from
  // `member_service.require_role(..., "viewer")`. The same bug bit
  // the export endpoints earlier (see `exportPython` / `exportJson`);
  // this is the runtime/continue equivalent.
  const res = await api.fetchRaw(`/api/v1/runtime/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      workflow_id: workflowId,
      input,
      session_id: sessionId ?? null,
    }),
    signal,
  })
  if (!res.ok || !res.body) {
    let msg = `HTTP ${res.status}`
    try {
      const body = await res.json()
      msg = body.detail ?? msg
    } catch { /* ignore */ }
    throw new Error(msg)
  }
  const sid = res.headers.get('x-session-id')
  await consumeSse(res.body, onEvent, signal)
  return sid ?? ''
}

export async function continueWorkflowStream(
  sessionId: string,
  response: string | boolean,
  onEvent: (ev: RuntimeEvent) => void,
  signal?: AbortSignal,
): Promise<string> {
  // Same X-User-Id fix as `runWorkflowStream` above — see the comment
  // there. The `/continue` endpoint is owner-scoped (multi-user
  // refactor), so dropping the header would 404 even if membership
  // weren't an issue.
  const res = await api.fetchRaw(`/api/v1/runtime/continue`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, response }),
    signal,
  })
  if (!res.ok || !res.body) {
    let msg = `HTTP ${res.status}`
    try {
      const body = await res.json()
      msg = body.detail ?? msg
    } catch { /* ignore */ }
    throw new Error(msg)
  }
  const sid = res.headers.get('x-session-id')
  await consumeSse(res.body, onEvent, signal)
  return sid ?? ''
}

/**
 * Cancel an in-flight run by `session_id`.
 *
 * The endpoint sets agno's `Workflow.cancel_run(run_id)` flag; the
 * currently-streaming SSE leg picks it up between agent chunks and
 * yields `WorkflowCancelledEvent` → `ErrorEvent("workflow cancelled")`.
 * The client therefore does NOT need to abort its in-flight `fetch`
 * — leaving the stream open lets us surface the cancellation message
 * naturally. Idempotent: hitting cancel on a completed/unknown
 * session is a 200 with `{cancelled: false}`.
 */
export async function cancelRuntime(sessionId: string): Promise<{ cancelled: boolean }> {
  const res = await api.fetchRaw(`/api/v1/runtime/${encodeURIComponent(sessionId)}/cancel`, {
    method: 'POST',
  })
  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try {
      const body = await res.json()
      msg = body.detail ?? msg
    } catch { /* ignore */ }
    throw new Error(msg)
  }
  return res.json() as Promise<{ cancelled: boolean }>
}

// / session: inspect a session's state on page load. The
// frontend uses this to detect a paused HITL session and rehydrate
// the `pendingConfirmation` chat state — without it, refreshing
// mid-pause loses the pending question + the user's in-flight
// answer input. The shape mirrors the slim session's serialised
// state from `runtime_service.get_session`.
export interface RuntimeSessionSnapshot {
  id: string
  workflow_id: string
  status: 'running' | 'waiting_confirmation' | 'completed' | 'error'
  input: string
  output: string | null
  history: RuntimeEvent[]
  // agno's `StepRequirement` objects (the same data the EventAdapter
  // translates into `ConfirmationEvent` for the in-stream path).
  // We don't pin the exact keys — they're agno-internal — but the
  // frontend can use `user_input_message` and `user_input_schema`
  // on each.
  pending_requirements: Array<Record<string, unknown>>
}
export async function getRuntimeSession(sessionId: string): Promise<RuntimeSessionSnapshot> {
  const res = await api.fetchRaw(`/api/v1/runtime/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'GET',
  })
  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try {
      const body = await res.json()
      msg = body.detail ?? msg
    } catch { /* ignore */ }
    throw new Error(msg)
  }
  return res.json() as Promise<RuntimeSessionSnapshot>
}

async function consumeSse(
  body: ReadableStream<Uint8Array>,
  onEvent: (ev: RuntimeEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    if (signal?.aborted) {
      await reader.cancel()
      return
    }
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let sep
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const chunk = buffer.slice(0, sep)
      buffer = buffer.slice(sep + 2)
      for (const line of chunk.split('\n')) {
        if (line.startsWith('data:')) {
          const payload = line.slice(5).trim()
          if (payload === '[DONE]') return
          try {
            onEvent(JSON.parse(payload) as RuntimeEvent)
          } catch (e) {
            console.warn('SSE parse error', e, payload)
          }
        }
      }
    }
  }
}