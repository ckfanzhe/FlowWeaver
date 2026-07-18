/**
 * Pure reducer for the chat-builder SSE stream.
 *
 * Mirrors `sseClient.ts` (the runtime's reducer) but for the
 * `BuilderEvent` shape. Returns patches the orchestration layer
 * forwards to the zustand store.
 *
 * Kept framework-free: no React, no zustand. The store layer
 * (`builderChatStore.ts`) owns the state and consumes the patches.
 *
 * The reducer is mostly append-only — each `BuilderEvent` pushes one
 * message into the transcript. The framework's `ToolGroup` slot
 * auto-wraps consecutive tool-call parts into a group, so we
 * don't need to coalesce them here.
 *
 * One exception: streaming `text` events carry `delta=true`. Each
 * delta is a fragment that should be APPENDED to the last text
 * bubble rather than opening a new bubble. The reducer signals
 * that intent via the `appendToLastText` patch — the orchestrator
 * decides whether to extend an existing text bubble or fall back
 * to creating a new one (e.g. if the previous bubble isn't a
 * text message).
 *
 * Note: `thinking` events produce a `reasoning` message. The
 * framework's `Reasoning:` slot renders it. Once a `text` /
 * `tool_call` / `diff` event lands, the message's status flips
 * to `complete` and the spinner disappears — no client-side
 * tracking needed.
 */
import type { BuilderEvent, BuilderDiff } from '../types/chatBuilder'

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
    | 'retry'
  data: Record<string, unknown>
}

export interface ReducerPatches {
  /** New messages to append to the transcript. */
  appendMessages?: BuilderChatMessage[]
  /** Append `content` to the last text bubble (creating one if
   *  the last bubble isn't `kind: 'text'`). Used by streaming
   *  text deltas. */
  appendToLastText?: { content: string }
  /** Set the session id (from the first `start` event). */
  sessionId?: string | null
  /** Set/replace the latest error message. */
  error?: string | null
  /** Replace the current diff (latest `diff` event wins). */
  diff?: BuilderDiff | null
  /** Mark the stream as finished (success or error). */
  finished?: boolean
}

export type IdFactory = () => string

/**
 * Apply a `BuilderEvent` and return the state patches.
 *
 * Behaviour:
 *   - `start` → set sessionId. The framework's runtime will surface
 *     a running-state indicator; we don't push a synthetic "thinking"
 *     bubble here (the framework's `MessageStatus: running` covers it).
 *   - `thinking` → push a `thinking` message. The adapter renders
 *     this as a native `ReasoningMessagePart` so the framework's
 *     `Reasoning:` slot handles the visual.
 *   - `text` (delta=false/absent) → push a `text` message.
 *   - `text` (delta=true) → emit an `appendToLastText` patch.
 *     The orchestrator appends `content` to the most recent text
 *     bubble (or creates one if the previous bubble isn't text).
 *     This is how the chat shows the LLM "typing" character by
 *     character instead of buffering for several seconds.
 *   - `tool_call` / `tool_result` → push `tool_call` / `tool_result`
 *     messages. The adapter renders both as native `ToolCallMessagePart`
 *     (with `result` set on the result variant). The framework's
 *     `ToolGroup` slot auto-wraps consecutive tool calls.
 *   - `diff` → replace the diff card and push a `diff` message.
 *   - `completed` → push a `completed` message, mark finished.
 *   - `error` → push an `error` message, mark finished. Preserve
 *     `diff` so the user can still apply the staged changes if
 *     the LLM hiccuped on a follow-up turn.
 */
export function reduceBuilderEvent(
  ev: BuilderEvent,
  nextId: IdFactory,
): ReducerPatches {
  switch (ev.type) {
    case 'start':
      return { sessionId: ev.session_id }
    case 'thinking': {
      const id = nextId()
      return {
        appendMessages: [{ id, kind: 'thinking', data: {} }],
      }
    }
    case 'text': {
      // Delta fragment — the LLM is mid-sentence. The orchestrator
      // will APPEND `content` to the last text bubble rather than
      // starting a new one. Without this branch the chat would
      // flash one bubble per delta (a flicker of "H", "Hell", "Hello")
      // and never give the typing illusion.
      if (ev.delta === true) {
        return { appendToLastText: { content: ev.content } }
      }
      const id = nextId()
      return {
        appendMessages: [{ id, kind: 'text', data: ev as unknown as Record<string, unknown> }],
      }
    }
    case 'tool_call': {
      const id = nextId()
      return {
        appendMessages: [{ id, kind: 'tool_call', data: ev as unknown as Record<string, unknown> }],
      }
    }
    case 'tool_result': {
      const id = nextId()
      return {
        appendMessages: [{ id, kind: 'tool_result', data: ev as unknown as Record<string, unknown> }],
      }
    }
    case 'diff': {
      const id = nextId()
      return {
        diff: { summary: ev.summary, nodes: ev.nodes, edges: ev.edges },
        appendMessages: [{ id, kind: 'diff', data: ev as unknown as Record<string, unknown> }],
      }
    }
    case 'completed': {
      const id = nextId()
      return {
        appendMessages: [{ id, kind: 'completed', data: ev as unknown as Record<string, unknown> }],
        finished: true,
      }
    }
    case 'error': {
      const id = nextId()
      return {
        appendMessages: [{ id, kind: 'error', data: ev as unknown as Record<string, unknown> }],
        error: ev.message,
        finished: true,
      }
    }
    case 'retry': {
      // Mid-stream retry — backend hit a transient SSE-parser
      // JSONDecodeError and is restarting the run. We push a
      // small `retry` chip but DO NOT mark the stream as finished
      // (the second attempt's events are still coming).
      const id = nextId()
      return {
        appendMessages: [{ id, kind: 'retry', data: ev as unknown as Record<string, unknown> }],
      }
    }
    default:
      // Unreachable — discriminated union exhaustiveness.
      return {}
  }
}
