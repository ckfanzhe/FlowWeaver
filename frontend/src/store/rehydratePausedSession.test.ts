/**
 * Tests for / session: rehydratePausedSession.
 *
 * Run:  npx tsx --test src/store/rehydratePausedSession.test.ts
 *
 * Pins the contract for the page-load rehydration path. A user
 * who refreshes mid-pause loses the chat store's
 * `pendingConfirmation`; this action calls
 * `GET /runtime/sessions/{sid}` and reconstructs the pending ask
 * from `pending_requirements` so the user can pick up where they
 * left off (instead of having to retype their question).
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import { useChatStore } from './chatStore'
import { rehydratePausedSession } from './chatActions'

// Mock fetch with canned responses per URL.
const originalFetch = globalThis.fetch
function mockFetch(
  queue: Array<{ match: (url: string) => boolean; respond: unknown }>,
) {
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url =
      typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.toString()
          : (input as Request).url
    for (const entry of queue) {
      if (entry.match(url)) {
        return new Response(JSON.stringify(entry.respond), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
    }
    return new Response('not mocked', { status: 404 })
  }) as typeof fetch
}

function reset() {
  useChatStore.setState({
    messages: [],
    sessionId: null,
    busy: false,
    error: null,
    pendingConfirmation: null,
  })
}

test('rehydratePausedSession: sets pendingConfirmation from server response', async () => {
  reset()
  useChatStore.setState({ sessionId: 'sess-1' })
  mockFetch([
    {
      match: (u) => u.endsWith('/api/v1/runtime/sessions/sess-1'),
      respond: {
        id: 'sess-1',
        workflow_id: 'wf-1',
        status: 'waiting_confirmation',
        input: 'x',
        output: null,
        history: [],
        pending_requirements: [
          {
            user_input_message: 'Your name?',
            user_input_schema: { choices: ['alice', 'bob'] },
          },
        ],
      },
    },
  ])
  try {
    await rehydratePausedSession('sess-1')
    const state = useChatStore.getState()
    assert.ok(state.pendingConfirmation, 'pendingConfirmation was not set')
    assert.equal(state.pendingConfirmation!.kind, 'ask')
    assert.equal(state.pendingConfirmation!.prompt, 'Your name?')
    assert.deepEqual(state.pendingConfirmation!.choices, ['alice', 'bob'])
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('rehydratePausedSession: no-op when session is not paused', async () => {
  reset()
  useChatStore.setState({ sessionId: 'sess-1' })
  mockFetch([
    {
      match: (u) => u.endsWith('/api/v1/runtime/sessions/sess-1'),
      respond: {
        id: 'sess-1',
        workflow_id: 'wf-1',
        status: 'completed',
        input: 'x',
        output: 'done',
        history: [],
        pending_requirements: [],
      },
    },
  ])
  try {
    await rehydratePausedSession('sess-1')
    const state = useChatStore.getState()
    assert.equal(state.pendingConfirmation, null,
      'completed session must not set pendingConfirmation')
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('rehydratePausedSession: no-op when paused session has no requirements', async () => {
  // Defensive — if the server says "waiting_confirmation" but the
  // list is empty, the runtime is in a weird state. Don't surface
  // a phantom ask to the user.
  reset()
  useChatStore.setState({ sessionId: 'sess-1' })
  mockFetch([
    {
      match: (u) => u.endsWith('/api/v1/runtime/sessions/sess-1'),
      respond: {
        id: 'sess-1',
        workflow_id: 'wf-1',
        status: 'waiting_confirmation',
        input: 'x',
        output: null,
        history: [],
        pending_requirements: [],
      },
    },
  ])
  try {
    await rehydratePausedSession('sess-1')
    const state = useChatStore.getState()
    assert.equal(state.pendingConfirmation, null)
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('rehydratePausedSession: no-op when busy (in-flight stream)', async () => {
  // Don't disturb an active in-flight run — rehydration is for
  // "user reloaded the page", not "stream is mid-RUN". If a
  // stream is open, the `confirmation` event will arrive in real
  // time and set pendingConfirmation via the normal in-stream
  // path. Overwriting from a stale GET would race the live
  // stream and clobber the in-flight answer input.
  reset()
  useChatStore.setState({ sessionId: 'sess-1', busy: true })
  let fetchCalled = false
  mockFetch([
    {
      match: () => {
        fetchCalled = true
        return true
      },
      respond: {},
    },
  ])
  try {
    await rehydratePausedSession('sess-1')
    assert.equal(fetchCalled, false,
      'rehydrate must not fetch when busy is true')
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('rehydratePausedSession: 404 swallowed silently (stale session id)', async () => {
  // A user who navigated to a different workflow has a stale
  // sessionId in the store. The GET 404s. We swallow so the
  // catch in App.tsx's workflow-load effect doesn't see a noisy
  // network error.
  reset()
  useChatStore.setState({ sessionId: 'sess-1' })
  globalThis.fetch = (async () =>
    new Response('not found', { status: 404 })) as typeof fetch
  try {
    // Should not throw.
    await rehydratePausedSession('sess-1')
    const state = useChatStore.getState()
    assert.equal(state.pendingConfirmation, null)
  } finally {
    globalThis.fetch = originalFetch
  }
})