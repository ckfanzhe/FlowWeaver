/**
 * ChatBuilder API client — mirrors `backend/src/app/api/chat_builder.py`.
 *
 * Three endpoints:
 *   - `streamBuilderChat(workflowId, messages, onEvent, signal)`
 *      — SSE stream of `BuilderEvent`s
 *   - `applyBuilderDiff(workflowId, sessionId, pending)`
 *      — JSON, commits the staged diff
 *   - `cancelBuilderDiff(sessionId)`
 *      — JSON, discards the session
 *
 * All calls go through `api.fetchRaw` / `api.post` so the `X-User-Id`
 * header is injected from localStorage — without it the backend
 * defaults to `user-default` and `member_service.require_role`
 * 403s the call.
 */
import { api } from './client'
import type {
  BuilderEvent,
  ChatBuilderApplyRequest,
  ChatBuilderRequest,
} from '../types/chatBuilder'
import type { Workflow } from '../types/workflow'

export interface RunBuilderHandle {
  sessionId: string | null
}

export async function streamBuilderChat(
  workflowId: string,
  messages: ChatBuilderRequest['messages'],
  onEvent: (ev: BuilderEvent) => void,
  options?: {
    /** Override the user's default LLM preset for this turn. When
     *  null/undefined the backend falls back to the default. */
    preset_id?: string | null
    signal?: AbortSignal
  },
): Promise<string> {
  // : must use `api.fetchRaw` (NOT raw `fetch`) so the
  // `X-User-Id` header is injected from localStorage. Same rationale
  // as `runWorkflowStream` — see the comment there.
  const body: ChatBuilderRequest = {
    workflow_id: workflowId,
    messages,
    preset_id: options?.preset_id ?? null,
  }
  const signal = options?.signal
  const res = await api.fetchRaw('/api/v1/chat/builder', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  if (!res.ok || !res.body) {
    let msg = `HTTP ${res.status}`
    try {
      const errBody = await res.json()
      msg = errBody.detail ?? msg
    } catch {
      /* ignore */
    }
    throw new Error(msg)
  }
  await consumeSse(res.body, onEvent, signal)
  // The session id is delivered as the first event's payload — the
  // caller grabs it from the `start` event. We don't rely on a
  // response header here because the streaming endpoint doesn't
  // expose one (the chat builder manages session state in-process,
  // not via the URL).
  return ''
}

export const builderApi = {
  apply: (workflowId: string, sessionId: string, pending: unknown[]) =>
    api.post<Workflow>('/api/v1/chat/builder/apply', {
      workflow_id: workflowId,
      session_id: sessionId,
      pending,
    } satisfies ChatBuilderApplyRequest),
  cancel: (workflowId: string, sessionId: string) =>
    api.post<{ discarded: boolean }>('/api/v1/chat/builder/cancel', {
      workflow_id: workflowId,
      session_id: sessionId,
      pending: [],
    }),
}

async function consumeSse(
  body: ReadableStream<Uint8Array>,
  onEvent: (ev: BuilderEvent) => void,
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
            onEvent(JSON.parse(payload) as BuilderEvent)
          } catch (e) {
            console.warn('BuilderChat SSE parse error', e, payload)
          }
        }
      }
    }
  }
}
