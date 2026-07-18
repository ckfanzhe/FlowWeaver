/**
 * BuilderChat types — mirrors backend `app.schemas.chat_builder`.
 *
 * The chat builder is a separate event stream from the runtime
 * (different `type` discriminator, different shape). Keeping the
 * two type unions side-by-side (not nested) lets the UI render them
 * with distinct visual treatments.
 */

/** What the client sends to `POST /api/v1/chat/builder`. */
export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface ChatBuilderRequest {
  workflow_id: string
  messages: ChatMessage[]
  /**
   * Optional override for the user's default LLM preset. When set,
   * the backend uses this preset instead of the default — used by
   * the chat header's model selector when the user picks a
   * non-default model for this turn (e.g. a stronger one for a
   * complex build). Backend validates the id; invalid overrides
   * fall back to the default silently.
   */
  preset_id?: string | null
}

export interface ChatBuilderApplyRequest {
  workflow_id: string
  session_id: string
  // The pending diff the user approved. Currently informational —
  // the server re-reads the session's staged state and re-validates.
  pending: unknown[]
}

/** SSE payload shape streamed by `POST /api/v1/chat/builder`. */
export type BuilderEvent =
  | { type: 'start'; session_id: string }
  | { type: 'thinking' }
  | { type: 'text'; content: string; delta?: boolean }
  | {
      type: 'tool_call'
      tool_call_id: string
      tool: string
      args: Record<string, unknown>
    }
  | {
      type: 'tool_result'
      tool_call_id: string
      tool: string
      ok: boolean
      message: string
      diff_summary?: Record<string, number> | null
    }
  | {
      type: 'diff'
      summary: Record<string, number>
      nodes: Array<{
        op: 'added' | 'removed' | 'updated'
        node?: Record<string, unknown>
        before?: Record<string, unknown>
        after?: Record<string, unknown>
      }>
      edges: Array<{
        op: 'added' | 'removed' | 'updated'
        edge?: Record<string, unknown>
        before?: Record<string, unknown>
        after?: Record<string, unknown>
      }>
    }
  | { type: 'completed'; output: string }
  | { type: 'error'; message: string }
  /** Mid-stream retry notice — the previous attempt hit a transient
   * SSE-parser JSONDecodeError and the backend is restarting the
   * LLM run. The user sees a tiny "stream interrupted, retrying…"
   * chip and the new attempt's events flow in below it. */
  | { type: 'retry'; reason: string }

/** Shape of the diff card the UI renders. */
export interface BuilderDiff {
  summary: Record<string, number>
  nodes: Array<{
    op: 'added' | 'removed' | 'updated'
    node?: Record<string, unknown>
    before?: Record<string, unknown>
    after?: Record<string, unknown>
  }>
  edges: Array<{
    op: 'added' | 'removed' | 'updated'
    edge?: Record<string, unknown>
    before?: Record<string, unknown>
    after?: Record<string, unknown>
  }>
}

/** A single bubble in the chat transcript. The kind tells the renderer
 * which template to use. Mirrors the runtime's `ChatMessage` shape
 * so the bubble components can be shared. */
export interface BuilderChatMessage {
  id: string
  kind:
    | 'user'
    | 'thinking'
    | 'text'
    | 'tool_call'
    | 'tool_result'
    | 'diff'
    | 'error'
    | 'completed'
    /** Mid-stream retry notice — the backend's first attempt hit
     * a transient SSE-parser JSONDecodeError and is restarting the
     * LLM run. Rendered as a small inline chip (no full bubble). */
    | 'retry'
  data: Record<string, unknown>
}
