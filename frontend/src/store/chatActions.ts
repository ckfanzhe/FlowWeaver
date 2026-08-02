/**
 * Chat send/answer orchestration.
 *
 * Wires together:
 *   - `sseClient.reduceRuntimeEvent` (pure reducer)
 *   - `useChatRunStore` (transcript + sessionId + busy + pendingConfirmation)
 *   - `useTraceStore` (telemetry)
 *   - `runWorkflowStream` / `continueWorkflowStream` (network)
 *
 * Kept separate from the store so:
 *   - The store stays single-responsibility.
 *   - We can swap the network layer (e.g. move to HTTP-batch) without
 *     touching the store.
 *   - Tests can drive the orchestrator directly with a mocked
 *     `runWorkflowStream`.
 *
 * The previous design glitched every "answer" through `send()`
 * because `pendingConfirmation` was derived rather than first-class.
 * That loop is now structurally impossible — see `dispatchSend` /
 * `dispatchAnswer`.
 */
import { runWorkflowStream, continueWorkflowStream, cancelRuntime } from '../api/workflows'
import { useTraceStore } from './traceStore'
import { reduceRuntimeEvent, type IdFactory } from './sseClient'
import { useChatRunStore } from './chatRunStore'

const idCounter = { v: 0 }
const nextId: IdFactory = () => `m-${++idCounter.v}`

/**
 * Apply a `RuntimeEvent` to all chat + trace stores. The single entry
 * point used by both `runWorkflowStream`'s callback and the trace
 * panel's re-run.
 */
export function feedRuntimeEvent(ev: Parameters<typeof reduceRuntimeEvent>[0]): void {
  const patches = reduceRuntimeEvent(ev, nextId)
  const run = useChatRunStore.getState()
  if (patches.appendMessages && patches.appendMessages.length > 0) {
    run.append(patches.appendMessages)
  }
  if ('sessionId' in patches) {
    run.setSessionId(patches.sessionId ?? null)
  }
  if ('error' in patches) {
    run.setError(patches.error ?? null)
  }
  if ('pendingConfirmation' in patches) {
    run.setPendingConfirmation(patches.pendingConfirmation ?? null)
  }
  if (patches.traceEvent) {
    useTraceStore.getState().apply(patches.traceEvent)
  }
}

/**
 * Start a fresh run for `workflowId`. Refuses to start while a stream
 * is in flight OR while a confirmation is pending — sending a fresh
 * run on top of a paused session would orphan the paused one.
 */
export async function dispatchSend(workflowId: string, input: string): Promise<void> {
  const run = useChatRunStore.getState()
  if (run.busy) return
  if (run.pendingConfirmation) return
  run.setBusy(true)
  run.setError(null)
  run.append([{ id: nextId(), kind: 'user', data: { text: input } }])
  // Reset the trace timeline so the canvas dots reflect THIS run, not
  // the previous one. The user typed input → run starts → status cleared.
  useTraceStore.getState().reset(input)
  try {
    const sessionId = await runWorkflowStream(
      workflowId,
      input,
      feedRuntimeEvent,
      run.sessionId ?? undefined,
      undefined,
    )
    // The backend's `X-Session-Id` is CORS-exposed by the server. If the
    // header is missing (proxies, misconfig), `sessionId` is "" and we
    // leave it null — `pendingConfirmation` still works because the
    // confirmation event carries its own sessionId.
    if (sessionId) run.setSessionId(sessionId)
  } catch (e) {
    useChatRunStore.setState({
      error: (e as Error).message,
      busy: false,
      pendingConfirmation: null,
    })
    return
  }
  run.setBusy(false)
}

/**
 * Reply to the pending confirmation. Refuses if no prompt is queued.
 * Reads the LIVE sessionId (not the snapshot taken when the prompt
 * arrived) because the SSE stream only writes the session id back
 * after consumption — see `chatStore.test.ts` for the regression
 * this guards.
 */
export async function dispatchAnswer(response: string | boolean): Promise<void> {
  const run = useChatRunStore.getState()
  if (run.busy || !run.pendingConfirmation) return
  const liveSessionId = run.sessionId
  if (!liveSessionId) {
    run.setError(
      'Session id missing — the backend did not expose X-Session-Id via CORS. Refresh and retry.'
    )
    run.setPendingConfirmation(null)
    return
  }
  run.setBusy(true)
  run.setError(null)
  run.append([{ id: nextId(), kind: 'user', data: { text: String(response) } }])
  // Clear pendingConfirmation BEFORE the stream so a fast double-click
  // can't re-route a second `answer()` while the first is in flight.
  run.setPendingConfirmation(null)
  try {
    await continueWorkflowStream(liveSessionId, response, feedRuntimeEvent)
  } catch (e) {
    run.setError((e as Error).message)
    run.setBusy(false)
    return
  }
  run.setBusy(false)
}

/**
 * Reset the entire chat — messages, session, error, pending
 * confirmation. Used by the toolbar "clear" button and by the workflow
 * store when a new workflow is loaded.
 *
 * / session : if there's an active slim session on
 * the backend, also POST to `/runtime/cancel` so the backend drops
 * its `_SESSIONS` entry. Without this, clicking Clear leaves an
 * orphaned slim session on the server — memory leak across many
 * sessions. (The previous behaviour only cleared the frontend
 * store.) The `cancelRuntime` call is fire-and-forget — we don't
 * `await` it because the user is already past the Clear button
 * and waiting for the network would just slow down the UI reset.
 * Errors are silently swallowed: if there's no active session on
 * the server (e.g. page refreshed mid-run, so the in-memory
 * store lost the session before the Clear click), the 404 is
 * expected.
 */
export function dispatchResetMessages(): void {
  const run = useChatRunStore.getState()
  const liveSessionId = run.sessionId
  if (liveSessionId) {
    // Fire-and-forget. catch() swallows 404 if the page was refreshed
    // mid-run and the in-memory store no longer has the session.
    void cancelRuntime(liveSessionId).catch(() => {})
  }
  run.resetAll()
}

/**
 * Cancel the in-flight run.
 *
 * No-op if nothing is running. Optimistically clears `busy` so the
 * composer unlocks immediately; the SSE stream's trailing
 * `error: "workflow cancelled"` event still arrives and feeds back
 * through `feedRuntimeEvent` for the chat transcript (the user sees
 * "workflow cancelled" appended to the chat as well). Race-safe:
 * if cancel arrives before `busy=false`, the store is already idle
 * and the call short-circuits.
 */
export async function dispatchCancel(): Promise<void> {
  const run = useChatRunStore.getState()
  if (!run.busy) return
  const liveSessionId = run.sessionId
  // Drop `busy` immediately so the UI unlocks. The SSE stream will
  // still surface the cancellation event when it catches up.
  run.setBusy(false)
  if (!liveSessionId) {
    // No session id — nothing to cancel server-side; the SSE stream
    // was already orphaned. Just clear local state.
    return
  }
  try {
    await cancelRuntime(liveSessionId)
  } catch (e) {
    // Surface the cancel error but don't re-throw — the SSE stream
    // will end either way and the user is already unblocked.
    run.setError((e as Error).message)
  }
}

/**
 * / session : rehydrate a paused HITL session
 * after a page refresh. Without this, a user who refreshes the
 * page mid-pause loses the pending question + their in-flight
 * answer input — the frontend `pendingConfirmation` is
 * `chatRunStore` only, and the store is fresh after refresh.
 *
 * The flow: page-load effect → for the active `sessionId`,
 * `GET /runtime/sessions/{sid}` returns the slim session state
 * (which carries `pending_requirements` populated by
 * `EventAdapter._capture_resume_state` when the
 * `WorkflowPausedEvent` originally landed). If status is
 * `waiting_confirmation`, we reconstruct a `PendingConfirmation`
 * from the most recent requirement and call
 * `setPendingConfirmation(...)` — the same shape the in-stream
 * `feedRuntimeEvent` reducer would have produced. The
 * `useChatStore.resetMessages` / `setPendingConfirmation` path
 * is one of the two contract paths the chat UI supports, so
 * the rendered pending-ask UI is identical to a never-refreshed
 * session.
 *
 * The function is fire-and-forget on the frontend — callers
 * (e.g. App.tsx's workflow-load effect) don't need to await it
 * because the state mutation is synchronous once the response
 * arrives. We swallow errors because a stale-session-id 404 is
 * expected when the user navigates to a different workflow.
 */
export async function rehydratePausedSession(sessionId: string): Promise<void> {
  const { getRuntimeSession } = await import('../api/workflows')
  const run = useChatRunStore.getState()
  // Don't disturb an active in-flight stream — rehydration is
  // for "user reloaded the page" cases, not "stream is mid-RUN".
  if (run.busy) return
  try {
    const snapshot = await getRuntimeSession(sessionId)
    if (snapshot.status !== 'waiting_confirmation') return
    if (!snapshot.pending_requirements.length) return
    // The most recent requirement is what the EventAdapter
    // would have translated into a `ConfirmationEvent`. Mirror
    // that shape here so the rehydration code can be identical
    // to the in-stream path.
    const req = snapshot.pending_requirements[0] as Record<string, unknown>
    // `user_input_message` / `user_input_schema` are agno's
    // standard fields on `StepRequirement`. They may be absent
    // (older agno versions) — fall back to generic placeholders
    // so the UI still renders SOMETHING.
    const prompt = typeof req.user_input_message === 'string'
      ? req.user_input_message
      : 'Workflow needs your input'
    const choices = extractChoices(req.user_input_schema)
    run.setPendingConfirmation({
      kind: 'ask',
      prompt,
      // `PendingConfirmation.choices` is `string[] | undefined`;
      // `extractChoices` returns `string[] | null` (None means
      // "no choice list"). Coerce `null` to `undefined` to drop
      // the field rather than store a sentinel.
      choices: choices ?? undefined,
      // Both fields are `string | undefined` / `string[] | undefined`
      // on `PendingConfirmation`. `null` would be a type error; omit.
      toolCallId: undefined,
    })
  } catch {
    // Stale session id (page navigated elsewhere) is expected —
    // the workflow-load effect's catch already handles fetch
    // errors. We swallow here to keep rehydration a
    // best-effort, side-effect-free operation.
  }
}

/** Extract the `choices` list from a StepRequirement's
 * `user_input_schema` payload, if any. The schema is agno-
 * internal so we don't pin its exact shape — we just look for
 * the conventional `choices: string[]` key. */
function extractChoices(schema: unknown): string[] | null {
  if (!schema || typeof schema !== 'object') return null
  const s = schema as Record<string, unknown>
  const raw = s.choices
  if (!Array.isArray(raw)) return null
  const out: string[] = []
  for (const v of raw) {
    if (typeof v === 'string') out.push(v)
  }
  return out.length ? out : null
}