/**
 * Regression tests for the chat store's SSE-event lifecycle + HITL
 * dispatcher.
 *
 * Run:  npx tsx --test src/store/chatStore.test.ts
 *
 * Why this file exists:
 *   The previous design derived `isPendingConfirm` from
 *   `lastMsg.kind === 'confirmation' && sessionId !== null` inside the
 *   React component. Combined with a backend CORS misconfig that hid
 *   `X-Session-Id` from JS, the dispatcher mis-routed every user
 *   "answer" as a fresh `send()`, looping the same `human_input` pause.
 *
 *   The fix: `pendingConfirmation` is now first-class store state, set
 *   on the `confirmation` event and cleared on `answer()` / `completed`
 *   / `error` / `resetMessages()`. These tests pin the contract.
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  dispatchCancel,
  feedRuntimeEvent,
  useChatStore,
  type ChatMessage,
} from './chatStore'
import type { RuntimeEvent } from '../types/workflow'

// ─────────────────────────────────────────────────────────────────
// fetch mock — captures every call's URL + body, returns canned SSE
// streams. Each test stubs `globalThis.fetch` and restores afterwards.
// ─────────────────────────────────────────────────────────────────
type FetchCall = {
  url: string
  body: Record<string, unknown>
}

interface FetchBehaviour {
  sessionId: string | null
  events: RuntimeEvent[]
  /** Override the HTTP status (default 200). */
  status?: number
}

const originalFetch = globalThis.fetch

function mockFetch(
  queue: Array<{ match: (call: FetchCall) => boolean; respond: FetchBehaviour }>,
) {
  const calls: FetchCall[] = []
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.toString()
          : (input as Request).url
    const body = init?.body ? JSON.parse(String(init.body)) : {}
    const call = { url, body }
    calls.push(call)
    for (const entry of queue) {
      if (entry.match(call)) {
        return makeResponse(entry.respond)
      }
    }
    // No match — return a 404 so the test fails loudly.
    return new Response('not mocked', { status: 404 })
  }) as typeof fetch
  return calls
}

function makeResponse(b: FetchBehaviour): Response {
  const body = encodeSse(b.events)
  return new Response(body, {
    status: b.status ?? 200,
    headers: {
      'Content-Type': 'text/event-stream',
      ...(b.sessionId ? { 'X-Session-Id': b.sessionId } : {}),
    },
  })
}

function encodeSse(events: RuntimeEvent[]): string {
  let out = ''
  for (const ev of events) {
    out += `data: ${JSON.stringify(ev)}\n\n`
  }
  out += 'data: [DONE]\n\n'
  return out
}

// Tiny helper to wait one microtask — Zustand updates land async-ish.
const tick = () => new Promise((r) => setTimeout(r, 0))

function resetStore() {
  useChatStore.setState({
    panelOpen: false,
    messages: [],
    sessionId: null,
    busy: false,
    error: null,
    pendingConfirmation: null,
  })
}

// ─────────────────────────────────────────────────────────────────
// Tests
// ─────────────────────────────────────────────────────────────────
test('confirmation event sets pendingConfirmation (first-class state)', async () => {
  resetStore()
  // Simulate a paused session: a node_start + a confirmation event
  // arriving DURING the SSE stream (sessionId in store is still null at
  // this point — it's set after the stream ends by runWorkflowStream).
  feedRuntimeEvent({
    type: 'node_start',
    nodeId: 'ask',
    nodeType: 'ask',
    label: 'Ask',
    t: 0,
  })
  feedRuntimeEvent({
    type: 'confirmation',
    kind: 'ask',
    prompt: 'What would you like to know?',
  })

  const state = useChatStore.getState()
  assert.ok(state.pendingConfirmation, 'pendingConfirmation must be set')
  assert.equal(state.pendingConfirmation!.kind, 'ask')
  assert.equal(state.pendingConfirmation!.prompt, 'What would you like to know?')
  // The last chat message is also the confirmation bubble.
  assert.equal(state.messages.at(-1)?.kind, 'confirmation')
})

test('send() is a no-op while pendingConfirmation is set (no forked run)', async () => {
  resetStore()
  feedRuntimeEvent({
    type: 'confirmation',
    kind: 'ask',
    prompt: 'Anything to add?',
  })
  assert.ok(useChatStore.getState().pendingConfirmation)

  const calls = mockFetch([
    {
      match: () => true,
      respond: { sessionId: 'should-not-be-called', events: [] },
    },
  ])
  try {
    await useChatStore.getState().send('wf-1', 'hello')
    assert.equal(
      calls.length,
      0,
      'send() must NOT dispatch a /run fetch while a confirmation is pending',
    )
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('answer() routes to /continue (not /run) and uses live sessionId', async () => {
  resetStore()
  // Drive the store into a "paused" state. We feed the confirmation
  // event first (as if /run streamed it), then the runWorkflowStream
  // call resolves and the caller writes sessionId.
  feedRuntimeEvent({
    type: 'confirmation',
    kind: 'ask',
    prompt: 'Your name?',
  })
  // Mimic runWorkflowStream's post-stream write:
  useChatStore.setState({ sessionId: 'sess-abc', busy: false })

  const calls = mockFetch([
    {
      // Only /continue should match.
      match: (c) => c.url.endsWith('/api/v1/runtime/continue'),
      respond: {
        sessionId: 'sess-abc',
        events: [
          {
            type: 'node_start',
            nodeId: 'ag',
            nodeType: 'agent',
            label: 'Agent',
            t: 10,
          },
          {
            type: 'node_end',
            nodeId: 'ag',
            status: 'ok',
            durationMs: 100,
            t: 110,
          },
          { type: 'text', content: 'hello back' },
          { type: 'completed', output: 'hello back' },
        ],
      },
    },
    {
      // /run should NEVER be called from answer().
      match: (c) => c.url.endsWith('/api/v1/runtime/run'),
      respond: {
        sessionId: 'should-not-happen',
        events: [],
        status: 500,
      },
    },
  ])
  try {
    await useChatStore.getState().answer('Alice')
    await tick()

    // Exactly one fetch, to /continue, with the live sessionId and
    // the user's answer as `response`.
    assert.equal(calls.length, 1, 'expected exactly one fetch')
    assert.ok(calls[0].url.endsWith('/api/v1/runtime/continue'))
    assert.deepEqual(calls[0].body, { session_id: 'sess-abc', response: 'Alice' })

    // The user's message was echoed into the transcript, pendingConfirmation
    // was cleared, and the workflow completed.
    // `sessionId` is INTENTIONALLY preserved on `completed` (
    // session — multi-turn context fix) so the next user message can
    // continue the same conversation by POSTing the same `session_id`.
    // See `sseClient.ts` for the rationale. node_start/node_end are
    // filtered out of the chat transcript — they go to the trace
    // store instead.
    const state = useChatStore.getState()
    assert.equal(state.pendingConfirmation, null)
    assert.equal(state.sessionId, 'sess-abc',
      'sessionId must be preserved across turns so the runtime '
      + 'reuses the same WorkflowSession (session multi-turn fix)')
    assert.equal(state.messages.at(-1)?.kind, 'completed')
    const kinds = state.messages.map((m: ChatMessage) => m.kind)
    assert.deepEqual(kinds.slice(-4), ['confirmation', 'user', 'text', 'completed'])
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('completed event clears pendingConfirmation (terminal state)', () => {
  resetStore()
  feedRuntimeEvent({ type: 'confirmation', kind: 'ask', prompt: 'x' })
  assert.ok(useChatStore.getState().pendingConfirmation)

  feedRuntimeEvent({ type: 'text', content: 'partial' })
  feedRuntimeEvent({ type: 'completed', output: 'partial' })

  assert.equal(
    useChatStore.getState().pendingConfirmation,
    null,
    'completed must clear any lingering prompt',
  )
  assert.equal(useChatStore.getState().sessionId, null)
})

test('error event keeps pendingConfirmation so user can retry the reply', () => {
  resetStore()
  feedRuntimeEvent({ type: 'confirmation', kind: 'ask', prompt: 'x' })
  assert.ok(useChatStore.getState().pendingConfirmation)

  feedRuntimeEvent({ type: 'error', message: 'backend exploded' })

  // The prompt is preserved — the user should be able to answer again
  // (or retry). Only `completed` / `answer()` / `resetMessages()` drop it.
  const state = useChatStore.getState()
  assert.ok(
    state.pendingConfirmation,
    'error mid-pause must keep the prompt so the user can retry',
  )
  assert.ok(state.error)
})

test('resetMessages() clears everything (pendingConfirmation included)', async () => {
  resetStore()
  feedRuntimeEvent({ type: 'confirmation', kind: 'ask', prompt: 'x' })
  useChatStore.setState({ sessionId: 'sess-1' })
  assert.ok(useChatStore.getState().pendingConfirmation)

  // session : resetMessages() now fires a fire-and-forget
  // POST to /runtime/{sid}/cancel so the backend tears down the
  // slim session too (the previous behaviour only cleared the
  // frontend store, leaking backend sessions across many cancels).
  // The mock below catches the cancel call so it doesn't surface
  // as an unhandled rejection when the test ends.
  const calls = mockFetch([
    {
      match: (c) => c.url.endsWith('/api/v1/runtime/sess-1/cancel'),
      respond: { sessionId: 'sess-1', events: [], status: 200 },
    },
  ])

  try {
    useChatStore.getState().resetMessages()
    // Give the fire-and-forget POST a microtask to run so the
    // assertion below is deterministic.
    await tick()

    const state = useChatStore.getState()
    assert.equal(state.pendingConfirmation, null)
    assert.equal(state.sessionId, null)
    assert.deepEqual(state.messages, [])
    // The cancel endpoint must have been hit with the live sid —
    // exactly one fetch, to /api/v1/runtime/sess-1/cancel.
    assert.equal(calls.length, 1, 'cancel endpoint must be hit')
    assert.ok(calls[0].url.endsWith('/api/v1/runtime/sess-1/cancel'))
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('answer() refuses when sessionId is missing (defence-in-depth)', async () => {
  resetStore()
  feedRuntimeEvent({ type: 'confirmation', kind: 'ask', prompt: 'x' })
  // sessionId is intentionally null (simulating a misconfigured CORS
  // environment that hid X-Session-Id from JS).
  assert.ok(useChatStore.getState().pendingConfirmation)

  const calls = mockFetch([
    {
      match: () => true,
      respond: { sessionId: 'should-not-be-called', events: [] },
    },
  ])
  try {
    await useChatStore.getState().answer('hi')
    assert.equal(
      calls.length,
      0,
      'answer() must NOT dispatch /continue without a session id',
    )
    const state = useChatStore.getState()
    assert.ok(
      state.error && /session id missing/i.test(state.error),
      'a clear error message guides the user to fix the CORS issue',
    )
    assert.equal(
      state.pendingConfirmation,
      null,
      'prompt is dropped after the error so the UI is not stuck',
    )
  } finally {
    globalThis.fetch = originalFetch
  }
})

// ─────────────────────────────────────────────────────────────────
// Cancel — dispatches POST /api/v1/runtime/{sid}/cancel, optimistically
// drops `busy`, and never throws even if the backend rejects (the SSE
// stream will end either way and the user is already unblocked).
// ─────────────────────────────────────────────────────────────────
test('cancel() is a no-op when nothing is in flight', async () => {
  resetStore()
  assert.equal(useChatStore.getState().busy, false)
  const calls = mockFetch([
    { match: () => true, respond: { sessionId: 'no', events: [] } },
  ])
  try {
    await dispatchCancel()
    assert.equal(calls.length, 0, 'no fetch fires when busy=false')
    assert.equal(useChatStore.getState().busy, false)
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('cancel() drops busy and POSTs to /api/v1/runtime/{sid}/cancel', async () => {
  resetStore()
  // Drive into a busy state with a known session id.
  useChatStore.setState({ busy: true, sessionId: 'sess-xyz' })

  const calls = mockFetch([
    {
      match: (c) =>
        c.url.endsWith('/api/v1/runtime/sess-xyz/cancel') &&
        (init_ => true),
      respond: { sessionId: null, events: [], status: 200 },
    },
  ])
  // Override `respond` shape — mockFetch's `respond` expects
  // SSE-shaped payload, but our cancel handler returns JSON.
  // Replace the queue with a JSON-only stub.
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.toString()
          : (input as Request).url
    calls.push({
      url,
      body: init?.body ? JSON.parse(String(init.body)) : {},
    })
    return new Response(JSON.stringify({ cancelled: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }) as typeof fetch

  try {
    await dispatchCancel()
    assert.equal(calls.length, 1, 'one cancel fetch')
    assert.ok(
      calls[0].url.endsWith('/api/v1/runtime/sess-xyz/cancel'),
      'hits the per-session cancel endpoint',
    )
    assert.deepEqual(calls[0].body, {}, 'no body — sid is in the URL')
    // Optimistic UI unblock.
    assert.equal(useChatStore.getState().busy, false)
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('cancel() surfaces backend errors without re-throwing', async () => {
  resetStore()
  useChatStore.setState({ busy: true, sessionId: 'sess-err' })

  const calls: FetchCall[] = []
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.toString()
          : (input as Request).url
    calls.push({
      url,
      body: init?.body ? JSON.parse(String(init.body)) : {},
    })
    return new Response(JSON.stringify({ detail: 'boom' }), {
      status: 500,
      headers: { 'Content-Type': 'application/json' },
    })
  }) as typeof fetch

  try {
    await dispatchCancel()
    // busy was already cleared optimistically before the fetch.
    assert.equal(useChatStore.getState().busy, false)
    // The error message lands on the store so the UI can render it.
    assert.match(useChatStore.getState().error ?? '', /boom/)
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('cancel() does nothing when sessionId is missing', async () => {
  resetStore()
  useChatStore.setState({ busy: true, sessionId: null })

  const calls: FetchCall[] = []
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.toString()
          : (input as Request).url
    calls.push({
      url,
      body: init?.body ? JSON.parse(String(init.body)) : {},
    })
    return new Response('not called', { status: 404 })
  }) as typeof fetch

  try {
    await dispatchCancel()
    assert.equal(calls.length, 0, 'no fetch when sid is missing')
    // busy was still dropped optimistically.
    assert.equal(useChatStore.getState().busy, false)
  } finally {
    globalThis.fetch = originalFetch
  }
})