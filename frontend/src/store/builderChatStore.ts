/**
 * ChatBuilder store — zustand state for the chat-creator panel.
 *
 * One unified store (vs the runtime's split-store pattern) because
 * the chat builder has fewer concerns: no per-session state, no
 * pending-confirmation pause, no trace forwarding. The pieces are:
 *
 *   - `messages`        — bubble transcript (user / LLM / tool /
 *                          diff / error). Append-only.
 *   - `sessionId`       — the in-memory chat session id (from the
 *                          first `start` event). Cleared on Apply
 *                          and on Cancel.
 *   - `busy`            — true while a stream is in flight.
 *   - `error`           — last unrecoverable error.
 *   - `diff`            — the latest diff event's payload. This
 *                          drives the [Apply] / [Cancel] buttons.
 *   - `finished`        — true when the stream completed (success or
 *                          error). Reset by the next `send` and by
 *                          Apply / Cancel.
 *
 * Actions (in `builderChatActions.ts`):
 *   - `dispatchSend(workflowId, message)` — append the user
 *     message, kick off the stream.
 *   - `dispatchApply()` — POST the staged diff; on success clear
 *     `diff` and `sessionId`.
 *   - `dispatchCancel()` — POST cancel; clear `diff` and `sessionId`.
 *   - `dispatchReset()` — clear everything (used by the toolbar
 *     "clear" button).
 *
 * The reducer for SSE events lives in `builderChatSseClient.ts`;
 * `feedBuilderEvent(ev)` is the single entry point both `dispatchSend`
 * and any future "send to all" affordance use.
 */
import { create } from 'zustand'
import type {
  BuilderChatMessage,
  BuilderDiff,
} from '../types/chatBuilder'
import {
  chatSessionKey,
  getBuildChat,
  putBuildChat,
  type BuildChatEnvelope,
} from '../lib/chatSessionStore'
import { useIdentityStore } from './identityStore'
import { useWorkflowStore } from './workflowStore'

export interface BuilderChatState {
  messages: BuilderChatMessage[]
  sessionId: string | null
  busy: boolean
  error: string | null
  diff: BuilderDiff | null
  finished: boolean
  /**
   * LLM preset the next send will use. `null` means "use the
   * server-side default"; a non-null id is forwarded to the
   * backend as `preset_id` and validated server-side (must exist
   * and belong to the caller / be system-shared).
   *
   * Stored on the builder chat (not on settings) because it's a
   * per-session UI choice — the user's system default preset stays
   * untouched. `dispatchSend` snapshots this at send time so
   * changing it mid-stream doesn't race.
   */
  selectedPresetId: string | null
}

export interface BuilderChatActions {
  appendMessages: (msgs: BuilderChatMessage[]) => void
  /**
   * Append `content` to the last text bubble. If the last
   * bubble is not `kind: 'text'`, append a NEW text bubble
   * carrying `content` (the first delta of a new turn has
   * no preceding text bubble to extend). Used by streaming
   * `text` events with `delta=true` — drives the "LLM is
   * typing" feel.
   */
  appendToLastText: (content: string) => void
  setSessionId: (sid: string | null) => void
  setBusy: (busy: boolean) => void
  setError: (err: string | null) => void
  setDiff: (diff: BuilderDiff | null) => void
  setFinished: (finished: boolean) => void
  setSelectedPresetId: (id: string | null) => void
  reset: () => void
}

export const useBuilderChatStore = create<BuilderChatState & BuilderChatActions>(
  (set) => ({
    messages: [],
    sessionId: null,
    busy: false,
    error: null,
    diff: null,
    finished: false,
    // Lazy default — the chat header's ModelSelector picks a real
    // preset (or null for "server default") once `useSettingsStore`
    // resolves. Keeping `null` here means a fresh tab doesn't
    // pretend to have a selection it doesn't actually have.
    selectedPresetId: null,
    appendMessages: (msgs) =>
      set((s) => ({ messages: [...s.messages, ...msgs] })),
    appendToLastText: (content) =>
      set((s) => {
        // Only extend the last bubble if it is ALREADY a text
        // bubble. Tool calls, thinking chips, diff cards, and
        // completed markers all break the streaming flow — if
        // the last bubble is one of those, the LLM started a
        // new utterance (e.g. "Done." after a tool call) and we
        // must open a fresh bubble. Otherwise we'd merge
        // unrelated statements into one paragraph ("Let me
        // add a router. Done." → "Let me add a router.Done.").
        // Index access (not `.at(-1)`) — the project's TS lib is
        // ES2020, which predates `Array.prototype.at`.
        const last = s.messages[s.messages.length - 1]
        if (!last || last.kind !== 'text') {
          const id = `bm-text-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
          return {
            messages: [
              ...s.messages,
              { id, kind: 'text', data: { content, delta: true } },
            ],
          }
        }
        const msgs = s.messages.slice()
        const prevContent = (last.data as { content?: string }).content ?? ''
        msgs[msgs.length - 1] = {
          ...last,
          data: { ...last.data, content: prevContent + content },
        }
        return { messages: msgs }
      }),
    setSessionId: (sessionId) => set({ sessionId }),
    setBusy: (busy) => set({ busy }),
    setError: (error) => set({ error }),
    setDiff: (diff) => set({ diff }),
    setFinished: (finished) => set({ finished }),
    setSelectedPresetId: (selectedPresetId) => set({ selectedPresetId }),
    reset: () =>
      set({
        messages: [],
        sessionId: null,
        busy: false,
        error: null,
        diff: null,
        finished: false,
        // Note: deliberately DON'T reset `selectedPresetId` —
        // clearing the chat shouldn't drop the user's model
        // choice. They'd have to re-pick it on every clear.
      }),
  }),
)

// ─────────────────────────────────────────────────────────────────
// Persistence: 300ms debounced auto-save to IndexedDB. Mirror of
// `chatRunStore`'s subscriber (same `chatSessionKey` helper for the
// composite `userId::workflowId` namespace, same debounce window).
//
// Persisted: `messages`, `diff`, `finished`, `selectedPresetId`.
// NOT persisted: `busy` (stream is dead on refresh), `error`
// (transient toast), `sessionId` (the builder's in-memory session
// id is server-scoped; the next `send` mints a new one).
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
  const s = useBuilderChatStore.getState()
  const env: BuildChatEnvelope = {
    key,
    messages: s.messages,
    diff: s.diff,
    finished: s.finished,
    selectedPresetId: s.selectedPresetId,
    savedAt: Date.now(),
  }
  putBuildChat(env).finally(() => {
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

if (typeof window !== 'undefined') {
  useBuilderChatStore.subscribe((state, prev) => {
    if (
      state.messages !== prev.messages ||
      state.diff !== prev.diff ||
      state.finished !== prev.finished ||
      state.selectedPresetId !== prev.selectedPresetId
    ) {
      _scheduleAutoSave()
    }
  })
}

/**
 * Restore the builder-chat transcript from IndexedDB. Called by
 * `App.tsx` after `loadFromBackend(workflowId)` resolves so the
 * composite key matches. `busy` / `error` are NOT restored (a
 * stream can't resume from a refresh and a stale error from before
 * the refresh shouldn't leak into the new session).
 */
export async function rehydrateChatBuilder(
  userId: string,
  workflowId: string | null,
): Promise<void> {
  const env = await getBuildChat(chatSessionKey(userId, workflowId))
  if (!env) return
  useBuilderChatStore.setState({
    messages: env.messages,
    diff: env.diff,
    finished: env.finished,
    selectedPresetId: env.selectedPresetId,
  })
}
