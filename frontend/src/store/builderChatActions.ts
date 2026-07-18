/**
 * ChatBuilder actions — orchestrator between the chat store, the
 * reducer, and the network layer.
 *
 * Pattern mirrors `chatActions.ts` (the runtime orchestrator):
 *   - `feedBuilderEvent` is the single entry point for SSE events.
 *   - `dispatchSend` opens the stream, pushing the user's
 *     message into the LLM-context history so the next turn
 *     includes it.
 *   - `dispatchApply` POSTs the staged diff; on success it
 *     invalidates the session and clears the diff.
 *   - `dispatchCancel` invalidates the session and clears the
 *     diff — the user can start a new chat immediately.
 *
 * The LLM-context history is held in a module-level list (NOT in
 * the zustand store — the bubbles are an internal presentation
 * detail; the LLM only sees the user/assistant text). The list
 * grows every turn: the user's message + the assistant's
 * `text` response (if any) get appended after each successful
 * stream. Tool calls are NOT pushed back into the LLM context —
 * the backend's `agent.run` keeps its own tool history via
 * agno's session state, so the client only needs to feed
 * user/assistant text.
 *
 * Errors / cancellations clear the partial user message from the
 * history so the next send starts with a clean slate.
 */
import { builderApi, streamBuilderChat } from '../api/chatBuilder'
import type {
  BuilderEvent,
  ChatMessage,
} from '../types/chatBuilder'
import type { Workflow } from '../types/workflow'
import { reduceBuilderEvent, type IdFactory } from './builderChatSseClient'
import { useBuilderChatStore } from './builderChatStore'

const idCounter = { v: 0 }
const nextId: IdFactory = () => `bm-${++idCounter.v}`

/** LLM-context history (user/assistant text only). Lives in
 * module scope so the bubbles don't accidentally leak into
 * the LLM context. Reset by `dispatchReset()`. */
const history: ChatMessage[] = []

/** AbortController for the in-flight SSE stream. Constructed in
 * `dispatchSend`, aborted in `dispatchCancel`. Lets the Stop
 * button cut the fetch immediately on the client side instead
 * of waiting for the LLM to finish its current turn. Stored at
 * module scope so the chat UI (anywhere in the tree) can reach
 * it without prop-drilling through the store.
 *
 * `null` between turns; replaced on every send. The matching
 * `dispatchCancel` is the only legitimate abort path — the
 * fetch's own error handler does NOT touch this. */
let currentAbort: AbortController | null = null

/** Optional callback fired after a successful `dispatchApply`.
 * Receives the freshly-applied `Workflow` (nodes + edges + id)
 * directly from the backend's apply response — no second GET
 * needed. App.tsx registers this on mount so the canvas picks
 * up the new state without prop-drilling through the chat
 * surface. */
let onAppliedListener: ((wf: Workflow) => void) | null = null

export function registerOnApplied(fn: (wf: Workflow) => void): () => void {
  onAppliedListener = fn
  return () => {
    if (onAppliedListener === fn) onAppliedListener = null
  }
}

/**
 * Apply a `BuilderEvent` to the chat store. Single entry point —
 * mirrors `feedRuntimeEvent` in `chatActions.ts`.
 */
export function feedBuilderEvent(ev: BuilderEvent): void {
  const patches = reduceBuilderEvent(ev, nextId)
  const store = useBuilderChatStore.getState()
  if (patches.appendMessages && patches.appendMessages.length > 0) {
    store.appendMessages(patches.appendMessages)
  }
  // Streaming text delta — append to the last text bubble
  // (creating one if the previous bubble isn't text). Fires
  // per LLM `RunContentEvent`, so the chat shows the LLM
  // typing token by token.
  if (patches.appendToLastText) {
    store.appendToLastText(patches.appendToLastText.content)
  }
  if (patches.sessionId !== undefined) {
    store.setSessionId(patches.sessionId)
  }
  if (patches.error !== undefined) {
    store.setError(patches.error)
  }
  if (patches.diff !== undefined) {
    store.setDiff(patches.diff)
  }
  if (patches.finished) {
    store.setFinished(true)
    store.setBusy(false)
  }
}

/**
 * Send a user message to the chat builder. Refuses to start if
 * a stream is in flight.
 *
 * Appends the user message to the LLM-context history before
 * opening the stream so even if the stream fails immediately the
 * next send picks up where this one left off.
 *
 * Also clears any stale `diff` from a previous turn: the new
 * turn may emit its own diff event, or it may legitimately
 * produce none (e.g. every tool call errored). Either way the
 * old Apply button must stop pointing at the old diff.
 */
export async function dispatchSend(workflowId: string, message: string): Promise<void> {
  const store = useBuilderChatStore.getState()
  if (store.busy) return
  if (!message.trim()) return
  // User-visible bubble first.
  store.appendMessages([
    { id: nextId(), kind: 'user', data: { text: message } },
  ])
  // Clear any stale diff card from the previous turn BEFORE we
  // start the new stream. The new turn may emit its own diff
  // (which replaces this) or none at all (in which case the
  // user shouldn't see dangling Apply buttons for old changes).
  store.setDiff(null)
  // LLM-context history (text only).
  history.push({ role: 'user', content: message })
  store.setBusy(true)
  store.setError(null)
  // Read the user's chosen model from the store at send time. We
  // snapshot it here (rather than passing through arguments) so
  // changing the dropdown after the user starts typing doesn't
  // race against the in-flight stream.
  const preset_id = store.selectedPresetId
  // Construct a fresh AbortController for this stream. The Stop
  // button reaches it via `dispatchCancel` → `currentAbort.abort()`.
  // The previous turn's controller (if any) was already aborted
  // or already settled — defensive `?.abort()` is a no-op there.
  const abort = new AbortController()
  currentAbort = abort
  try {
    await streamBuilderChat(
      workflowId,
      [...history],
      feedBuilderEvent,
      { preset_id, signal: abort.signal },
    )
    // After a successful stream, fold the latest assistant text
    // (if any) into the LLM history so the next turn sees it.
    // We don't push tool calls — the backend's agent.run keeps
    // its own tool transcript.
    const lastAssistantText = findLastAssistantText(store)
    if (lastAssistantText) {
      history.push({ role: 'assistant', content: lastAssistantText })
    }
  } catch (e) {
    // Aborted by the Stop button (not a real error) — the cancel
    // path already cleared busy/diff/sessionId; don't surface it
    // as a chat error and don't drop the history entry either
    // (the user might want to retry with the same prompt).
    if ((e as Error).name === 'AbortError') {
      return
    }
    store.setError((e as Error).message)
    store.setBusy(false)
    store.setFinished(true)
    // Drop the user message from the LLM history so the next
    // send starts fresh — the partial ask is in the bubble
    // trail so the user can rephrase.
    history.pop()
  } finally {
    // Clear the controller only if it's still ours — the cancel
    // path nulls it out, and a stray clear there is harmless.
    if (currentAbort === abort) {
      currentAbort = null
    }
  }
}

/**
 * Apply the staged diff to the workflow row. Re-validates on the
 * server side; on success clears the local diff + sessionId and
 * notifies the host via `onAppliedListener` so the canvas can
 * refresh.
 *
 * The backend re-reads the session's pending_changes and replays
 * them against the current DB row. If the latest turn produced
 * no new changes (every tool call errored), the backend is a
 * no-op but still returns success — we detect that here and
 * surface a clear "nothing to apply" message so the user
 * understands the button worked.
 *
 * The apply endpoint returns the freshly-updated `Workflow`
 * (nodes/edges included). We forward it to the listener so the
 * canvas can refresh locally without a second GET — this is
 * the "lightweight" refresh path.
 */
export async function dispatchApply(workflowId: string): Promise<{ ok: boolean; error?: string; empty?: boolean }> {
  const store = useBuilderChatStore.getState()
  if (!store.sessionId) {
    return { ok: false, error: 'No active chat session — send a message first.' }
  }
  const diff = store.diff
  const hasChanges = diff ? diffSummaryTotal(diff) > 0 : false
  if (!hasChanges) {
    // Don't hit the network — there's nothing to apply. Clear
    // the dangling diff card and report back so the UI can show
    // a "no changes" acknowledgement.
    store.setDiff(null)
    store.appendMessages([
      { id: nextId(), kind: 'completed', data: { text: 'No changes to apply.' } },
    ])
    return { ok: true, empty: true }
  }
  try {
    // The backend's apply response is the new authoritative
    // workflow state — feed it straight into the canvas store.
    // Saves a round-trip vs. calling `GET /workflows/{id}` after.
    const wf = await builderApi.apply(workflowId, store.sessionId, [])
    store.setDiff(null)
    store.setSessionId(null)
    // Keep the bubble trail so the user can see what they did.
    store.appendMessages([
      { id: nextId(), kind: 'completed', data: { text: 'Applied to workflow.' } },
    ])
    if (onAppliedListener) onAppliedListener(wf)
    return { ok: true }
  } catch (e) {
    return { ok: false, error: (e as Error).message }
  }
}

/**
 * Cancel the in-flight session without applying.
 *
 * Two-sided cleanup:
 *  1. CLIENT: abort the SSE fetch via `currentAbort` so the
 *     reader closes immediately and the UI frees. This is what
 *     makes the Stop button feel responsive — the network is
 *     cut, not just the backend's willingness to send events.
 *  2. SERVER: POST to /api/chat/builder/cancel so the backend
 *     flips the session's `cancel_requested` flag and `_consume_stream`
 *     breaks out of the agent's event loop on the next event.
 *     Without this, the LLM call on the server keeps running
 *     (just no longer reaching the client) until the turn
 *     finishes naturally.
 *
 * After both: clear local diff / sessionId / finished / busy so
 * the user can immediately start a new chat.
 */
export async function dispatchCancel(workflowId: string): Promise<void> {
  // Client-side abort FIRST — this is what gives the Stop button
  // its snap. The fetch's reader sees `signal.aborted = true`
  // on its next loop iteration and cancels cleanly. Doing this
  // before the network round-trip also means a slow / hung
  // server can't block the UI from snapping to idle.
  if (currentAbort) {
    try {
      currentAbort.abort()
    } catch {
      // already aborted — fine
    }
    currentAbort = null
  }
  const store = useBuilderChatStore.getState()
  if (store.sessionId) {
    try {
      await builderApi.cancel(workflowId, store.sessionId)
    } catch {
      // best-effort — the server will GC the session on next send
    }
  }
  store.setDiff(null)
  store.setSessionId(null)
  store.setFinished(false)
  store.setBusy(false)
}

/**
 * Clear the entire chat — bubbles, session, error, diff. Also
 * wipes the LLM-context history so the next chat starts fresh.
 */
export function dispatchReset(): void {
  history.length = 0
  useBuilderChatStore.getState().reset()
}

function findLastAssistantText(state: BuilderChatState): string | null {
  for (let i = state.messages.length - 1; i >= 0; i--) {
    const m = state.messages[i]
    if (m.kind === 'text') {
      const content = (m.data as { content?: string }).content
      if (content) return content
    }
  }
  return null
}

/** Sum every numeric field in the diff summary. Mirrors the
 *  `total` calculation in `DiffCard` — keep them in sync. */
function diffSummaryTotal(diff: { summary?: Record<string, number> }): number {
  const s = diff.summary ?? {}
  return (
    (s.added_nodes ?? 0) +
    (s.removed_nodes ?? 0) +
    (s.updated_nodes ?? 0) +
    (s.added_edges ?? 0) +
    (s.removed_edges ?? 0)
  )
}

// Type alias so the file's internal helpers see the same shape
// the store exposes (avoids a circular import).
type BuilderChatState = ReturnType<typeof useBuilderChatStore.getState>
