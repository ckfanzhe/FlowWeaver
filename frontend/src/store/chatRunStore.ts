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
 */
import { create } from 'zustand'
import type { ChatMessage, PendingConfirmation } from './sseClient'

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