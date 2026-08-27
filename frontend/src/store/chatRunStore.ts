/**
 * Runtime-mode chat store — merged from `chatSessionStore` + `chatMessagesStore`.
 *
 * Owns everything that drives the Run tab on the chat sidebar:
 *
 *   - `messages`               — append-only transcript.
 *   - `sessionId`              — current SSE session id (or null when idle).
 *   - `busy`                   — a stream is in flight.
 *   - `error`                  — last error message (cleared on next send/answer).
 *   - `pendingConfirmation`    — first-class prompt state. Non-null while
 *                                the workflow is paused awaiting user input.
 *
 * Why one store, not two:
 *   - Run mode has one orchestrator (`chatActions.ts::dispatchSend /
 *     dispatchAnswer / dispatchResetMessages / feedRuntimeEvent`) that
 *     reads+writes BOTH the session slice AND the transcript together
 *     (e.g. `dispatchSend` appends a `user` message THEN sets `busy`).
 *     Two stores forced every call site to thread both — and to reason
 *     about transactional consistency across them.
 *   - There is no consumer that needs to subscribe to "just the
 *     transcript" or "just the session" — `ChatSidebar.tsx` reads both
 *     from the same render path; `useTraceStore` and SSE reducer push
 *     into the orchestrator instead.
 *   - The Build mode (`builderChatStore.ts`) is already a single
 *     unified store — keeping Run mode split was the asymmetry.
 *
 * Migrated from the prior split stores (commit <see git log>) so the
 * surface is now: `useChatRunStore` only. The legacy names
 * `useChatSessionStore` + `useChatMessagesStore` are gone; consumers
 * updated to read the merged store. `chatStore.ts` (facade) keeps the
 * pre-split `useChatStore` API intact for the ~30 selectors already in
 * the codebase.
 *
 * ─────────────────────────────────────────────────────────────────
 * Chat persistence (page-refresh survival)
 * ─────────────────────────────────────────────────────────────────
 * The transcript, sessionId, and pendingConfirmation are mirrored to
 * IndexedDB on every change via a 300ms debounced auto-save (see
 * `chatSessionStore.ts`). `busy` and the live stream id are NOT
 * persisted: a refresh kills the SSE connection anyway, and resuming
 * a paused run is `rehydratePausedSession`'s job (it reads
 * `GET /runtime/sessions/{sid}` and reconstructs `pendingConfirmation`
 * from `pending_requirements`).
 *
 * The composite key `userId::workflowId` (or `userId::draft` for the
 * un-saved canvas) namespaces the write so user B can sign in on the
 * same browser and not see user A's chat — even though both live in
 * the same IndexedDB store.
 */
import { create } from 'zustand'
import type { ChatMessage, PendingConfirmation } from './sseClient'
import {
  chatSessionKey,
  getRunChat,
  putRunChat,
  type RunChatEnvelope,
} from '../lib/chatSessionStore'
import { useIdentityStore } from './identityStore'
import { useWorkflowStore } from './workflowStore'

interface State {
  messages: ChatMessage[]
  sessionId: string | null
  busy: boolean
  error: string | null
  pendingConfirmation: PendingConfirmation | null
}

interface Actions {
  /** Append one or more messages (most reducer calls return one). */
  append: (msgs: ChatMessage[]) => void
  setSessionId: (id: string | null) => void
  setBusy: (b: boolean) => void
  setError: (e: string | null) => void
  setPendingConfirmation: (p: PendingConfirmation | null) => void
  /** Reset the transcript only (called by reducer patches, e.g. on error). */
  resetMessages: () => void
  /** Reset to idle — clear sessionId, busy, error, pendingConfirmation. */
  resetSession: () => void
  /** Reset BOTH transcript and session — used by `dispatchResetMessages` and `signOut`. */
  resetAll: () => void
}

export const useChatRunStore = create<State & Actions>((set) => ({
  messages: [],
  sessionId: null,
  busy: false,
  error: null,
  pendingConfirmation: null,

  append: (msgs) =>
    set((s) => ({ messages: [...s.messages, ...msgs] })),
  setSessionId: (id) => set({ sessionId: id }),
  setBusy: (b) => set({ busy: b }),
  setError: (e) => set({ error: e }),
  setPendingConfirmation: (p) => set({ pendingConfirmation: p }),

  resetMessages: () => set({ messages: [] }),
  resetSession: () =>
    set({
      sessionId: null,
      busy: false,
      error: null,
      pendingConfirmation: null,
    }),
  resetAll: () =>
    set({
      messages: [],
      sessionId: null,
      busy: false,
      error: null,
      pendingConfirmation: null,
    }),
}))

// ─────────────────────────────────────────────────────────────────
// Persistence: 300ms debounced auto-save to IndexedDB.
//
// One subscriber, one timer. The subscriber fires on every store
// change but only arms the debounce when one of the *persisted*
// fields actually changed (the `busy` flag — a stream-state — does
// not, intentionally). The body itself is the only writer; re-arms
// during a burst coalesce into one write after the user goes idle.
//
// Identity is captured fresh at save time (`useIdentityStore
// .getState()`) so a sign-in between state changes still writes
// under the new userId, and `workflowId` is captured from
// `useWorkflowStore.getState()` so switching workflows targets a
// different composite key.
//
// We deliberately do NOT persist `busy` (the stream is dead on
// refresh) and NOT `error` (transient toast — refilling the chat
// with a stale error on page load is more confusing than helpful).
// ─────────────────────────────────────────────────────────────────
const CHAT_SAVE_DEBOUNCE_MS = 300

let autoSaveTimer: ReturnType<typeof setTimeout> | null = null
let autoSaveInFlight = false
let autoSavePending = false

function _resolveKey(): string | null {
  const userId = useIdentityStore.getState().userId
  if (!userId) return null
  const workflowId = useWorkflowStore.getState().workflowId
  return chatSessionKey(userId, workflowId)
}

function _runAutoSave(): void {
  if (autoSaveInFlight) {
    autoSavePending = true
    return
  }
  const key = _resolveKey()
  if (!key) return
  autoSaveInFlight = true
  // Snapshot the persisted fields. `busy` / `error` excluded
  // intentionally — see file header.
  const s = useChatRunStore.getState()
  const env: RunChatEnvelope = {
    key,
    messages: s.messages,
    sessionId: s.sessionId,
    pendingConfirmation: s.pendingConfirmation,
    savedAt: Date.now(),
  }
  putRunChat(env).finally(() => {
    autoSaveInFlight = false
    if (autoSavePending) {
      autoSavePending = false
      _scheduleAutoSave()
    }
  })
}

function _scheduleAutoSave(): void {
  if (autoSaveTimer !== null) clearTimeout(autoSaveTimer)
  autoSaveTimer = setTimeout(() => {
    autoSaveTimer = null
    _runAutoSave()
  }, CHAT_SAVE_DEBOUNCE_MS)
}

// `typeof window` guard — the 300ms debounce timer keeps the Node
// event loop alive past the last test if the subscriber runs in
// fake-indexeddb-backed test environments. Browser-only persistence
// is the right scope anyway (Node SSR isn't a target for this app).
// Mirrors the same guard `workflowStore.ts:612` uses for the
// `beforeunload` listener.
if (typeof window !== 'undefined') {
  useChatRunStore.subscribe((state, prev) => {
    // Only fire when one of the persisted fields actually changed.
    // SSE text-delta can fire `append` hundreds of times per turn;
    // the reference-equality guards keep the subscriber cheap and
    // the debounce collapses the burst into a single write.
    if (
      state.messages !== prev.messages ||
      state.sessionId !== prev.sessionId ||
      state.pendingConfirmation !== prev.pendingConfirmation
    ) {
      _scheduleAutoSave()
    }
  })
}

/**
 * Restore the run-chat transcript from IndexedDB. Called by
 * `App.tsx` after `loadFromBackend(workflowId)` resolves so the
 * composite key matches. `busy` is forced false (a stream can't
 * resume from a refresh; the user has to send a new message), and
 * `error` is cleared (a stale error from before the refresh
 * shouldn't leak into the new session). If no envelope exists
 * for the key, this is a no-op.
 */
export async function rehydrateChatRun(
  userId: string,
  workflowId: string | null,
): Promise<void> {
  const env = await getRunChat(chatSessionKey(userId, workflowId))
  if (!env) return
  useChatRunStore.setState({
    messages: env.messages,
    sessionId: env.sessionId,
    // busy / error intentionally not restored (see docstring)
    pendingConfirmation: env.pendingConfirmation,
  })
}