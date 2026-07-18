/**
 * Chat store — façade over `useChatRunStore` + `useAppUiStore` so the
 * ~30 existing selectors (`useChatStore((s) => s.x)`) keep working
 * after the SPEC .C split into per-concern stores.
 *
 * Module map (each owns one concern):
 *   - `sseClient.ts`        — pure event reducer (no React state)
 *   - `chatRunStore.ts`     — transcript + sessionId + busy + pendingConfirmation
 *   - `appUiStore.ts`       — templatesOpen + chat panelOpen / position / size
 *   - `chatActions.ts`      — send/answer/reset orchestration
 *   - `builderChatStore.ts` — Build mode (chat-builder) — separate flow
 *
 * Invariant: `pendingConfirmation` is first-class state on
 * `useChatRunStore` — the dispatcher (`chatActions.dispatchAnswer`)
 * reads ONE piece of state and routes deterministically, so a
 * `ask` pause cannot loop. See memory `chat-builder-f5-run`
 * for the original CORS / async-sessionId fix.
 */
import { useChatRunStore } from './chatRunStore'
import { useAppUiStore } from './appUiStore'
import {
  dispatchAnswer,
  dispatchCancel,
  dispatchResetMessages,
  dispatchSend,
  feedRuntimeEvent as feedRuntimeEventImpl,
} from './chatActions'

export { dispatchCancel }
export type { ChatMessage, PendingConfirmation } from './sseClient'

type RunState = ReturnType<typeof useChatRunStore.getState>
type UiState = ReturnType<typeof useAppUiStore.getState>

interface AggregateActions {
  togglePanel: () => void
  showPanel: () => void
  hidePanel: () => void
  resetMessages: () => void
  send: (workflowId: string, input: string) => Promise<void>
  answer: (response: string | boolean) => Promise<void>
  cancel: () => Promise<void>
}

/**
 * React-hook view combining `useChatRunStore` + `useAppUiStore`.
 * Each sub-hook triggers the right re-renders when its slice changes,
 * so existing selectors like `useChatStore((s) => s.messages)` work.
 */
export function useChatStore<T>(
  selector: (s: RunState & UiState & AggregateActions) => T,
): T {
  const messages = useChatRunStore((s) => s.messages)
  const sessionId = useChatRunStore((s) => s.sessionId)
  const busy = useChatRunStore((s) => s.busy)
  const error = useChatRunStore((s) => s.error)
  const pendingConfirmation = useChatRunStore((s) => s.pendingConfirmation)
  const panelOpen = useAppUiStore((s) => s.panelOpen)
  const ui = useAppUiStore.getState()
  return selector({
    messages,
    sessionId,
    busy,
    error,
    pendingConfirmation,
    panelOpen,
    togglePanel: ui.togglePanel,
    showPanel: ui.showPanel,
    hidePanel: ui.hidePanel,
    templatesOpen: ui.templatesOpen,
    openTemplates: ui.openTemplates,
    closeTemplates: ui.closeTemplates,
    position: ui.position,
    size: ui.size,
    setPanelPosition: ui.setPanelPosition,
    setPanelSize: ui.setPanelSize,
    resetPanelBounds: ui.resetPanelBounds,
    append: useChatRunStore.getState().append,
    setSessionId: useChatRunStore.getState().setSessionId,
    setBusy: useChatRunStore.getState().setBusy,
    setError: useChatRunStore.getState().setError,
    setPendingConfirmation: useChatRunStore.getState().setPendingConfirmation,
    resetMessages: dispatchResetMessages,
    send: dispatchSend,
    answer: dispatchAnswer,
    cancel: dispatchCancel,
  } as RunState & UiState & AggregateActions)
}

// Imperative access used by tests / non-React callers.
// `useChatStore.getState()` and `useChatStore.setState(...)` must keep
// the pre-facade shape so existing tests pass.
useChatStore.getState = () => {
  const run = useChatRunStore.getState()
  const ui = useAppUiStore.getState()
  return {
    ...run,
    ...ui,
    resetMessages: dispatchResetMessages,
    send: dispatchSend,
    answer: dispatchAnswer,
    cancel: dispatchCancel,
  }
}

useChatStore.setState = (partial: Partial<RunState & UiState>) => {
  const runPatch: Partial<RunState> = {}
  const uiPatch: Partial<UiState> = {}

  if ('messages' in partial) {
    if (partial.messages === undefined) {
      useChatRunStore.getState().resetMessages()
    } else {
      runPatch.messages = partial.messages
    }
  }
  if ('sessionId' in partial) runPatch.sessionId = partial.sessionId ?? null
  if ('busy' in partial) runPatch.busy = partial.busy ?? false
  if ('error' in partial) runPatch.error = partial.error ?? null
  if ('pendingConfirmation' in partial) {
    runPatch.pendingConfirmation = partial.pendingConfirmation ?? null
  }
  if ('panelOpen' in partial) uiPatch.panelOpen = partial.panelOpen ?? false

  if (Object.keys(runPatch).length > 0) useChatRunStore.setState(runPatch)
  if (Object.keys(uiPatch).length > 0) useAppUiStore.setState(uiPatch)
}

/**
 * Public event-dispatch entry point. The trace panel uses this when
 * it kicks off a re-run so the chat panel sees the resulting events
 * without having to call `send()` (which expects a workflow_id and
 * a fresh input).
 */
export function feedRuntimeEvent(ev: Parameters<typeof feedRuntimeEventImpl>[0]): void {
  feedRuntimeEventImpl(ev)
}