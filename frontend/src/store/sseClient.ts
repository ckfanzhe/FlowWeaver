/**
 * Pure SSE protocol reducer — given a `RuntimeEvent`, compute the
 * state deltas (chat messages, trace signals, session-id updates,
 * pending-confirmation flips).
 *
 * Why split this out of `chatStore`:
 *   - Testable with no React / no zustand — feed events in, assert the
 *     returned patches.
 *   - Reusable: the trace panel kicks off its own runs and feeds the
 *     resulting events through `feedRuntimeEvent`; this is the single
 *     place that decides how an event mutates state.
 *   - When we eventually swap SSE for WebSockets (or batched HTTP),
 *     only this module needs to change.
 *
 * The reducer returns a `Patches` object that callers forward to their
 * store's `setState`. No direct store coupling.
 */
import type { RuntimeEvent } from '../types/workflow'

export interface ChatMessage {
  id: string
  kind: 'user' | 'text' | 'tool_call' | 'tool_result' | 'confirmation' | 'error' | 'completed'
  data: Record<string, unknown>
}

/** A paused-session prompt awaiting the user's reply. */
export interface PendingConfirmation {
  kind: 'tool_confirm' | 'ask'
  prompt: string
  choices?: string[]
  toolCallId?: string
}

/**
 * What `reduceRuntimeEvent` returns. All fields are optional — most
 * events only touch one or two of them.
 */
export interface ReducerPatches {
  /** New messages to append to the chat transcript. */
  appendMessages?: ChatMessage[]
  /** Set/replace the current `sessionId` (header from the SSE stream). */
  sessionId?: string | null
  /** Set/replace the latest error message. */
  error?: string | null
  /** Set/replace the pending confirmation prompt. */
  pendingConfirmation?: PendingConfirmation | null
  /**
   * Fired by trace-only events (`node_start` / `node_end`).
   * Callers forward this to the trace store.
   */
  traceEvent?: RuntimeEvent
}

/** Monotonic id factory — passed in so the reducer stays pure. */
export type IdFactory = () => string

/**
 * Apply a `RuntimeEvent` and return the state patches.
 *
 * Behavior contract (preserved from the pre-split chatStore):
 *   - Trace-only events (`node_start` / `node_end`) do NOT push a
 *     chat message — they only emit `traceEvent` for the trace store.
 *   - `completed` clears `pendingConfirmation` only — it PRESERVES
 *     `sessionId` so the next user message can continue the same
 *     agent conversation. ( fix — the previous behavior
 *     dropped the sessionId here, which caused every follow-up turn
 *     to mint a brand-new slim session + brand-new WorkflowSession
 *     + brand-new AgentSession, wiping all prior tool calls /
 *     tool results. The user-visible symptom: "".
 *     Multi-turn continuation now works end-to-end: turn #2 POSTs
 *     the prior `session_id` and the runtime reuses the same
 *     `Wf.run(session_id=...)` chain so prior agent messages stay
 *     in the context window.)
 *   - `error` PRESERVES `pendingConfirmation` so the user can retry.
 *   - `confirmation` flips `pendingConfirmation` to the new prompt.
 *   - All other events push a single message.
 */
export function reduceRuntimeEvent(ev: RuntimeEvent, nextId: IdFactory): ReducerPatches {
  const traceEvent = ev

  if (ev.type === 'node_start' || ev.type === 'node_end') {
    return { traceEvent }
  }

  const id = nextId()

  if (ev.type === 'completed') {
    // Preserve sessionId — see the contract above. Clearing only
    // pendingConfirmation lets the user see "completed" in the
    // transcript and then start a fresh turn on the same session
    // (the chat input stays unlocked because pendingConfirmation
    // went null and `busy` was already false by the time the
    // completed event landed).
    return {
      appendMessages: [{ id, kind: 'completed', data: ev as unknown as Record<string, unknown> }],
      pendingConfirmation: null,
      traceEvent,
    }
  }
  if (ev.type === 'error') {
    return {
      appendMessages: [{ id, kind: 'error', data: ev as unknown as Record<string, unknown> }],
      error: ev.message,
      // Preserve pendingConfirmation so the user can retry.
      traceEvent,
    }
  }
  if (ev.type === 'confirmation') {
    return {
      appendMessages: [{ id, kind: 'confirmation', data: ev as unknown as Record<string, unknown> }],
      pendingConfirmation: {
        kind: ev.kind,
        prompt: ev.prompt,
        choices: ev.choices,
        toolCallId: ev.toolCallId,
      },
      traceEvent,
    }
  }
  // text / tool_call / tool_result
  return {
    appendMessages: [{
      id,
      kind: ev.type as ChatMessage['kind'],
      data: ev as unknown as Record<string, unknown>,
    }],
    traceEvent,
  }
}