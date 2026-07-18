/**
 * Tests for the runtime SSE reducer (`sseClient.ts`).
 *
 * Run:  npx tsx --test src/store/sseClient.test.ts
 *
 * Background: the reducer turns `RuntimeEvent`s into `Patches` the
 * store forwards to `setState`. The contract under test is the
 * multi-turn context-preservation contract — specifically the
 *  fix where the `completed` event MUST NOT actively
 * clear the sessionId, otherwise the next user message can never
 * continue the same conversation (the chat-run POST would carry
 * `session_id=null` and the backend would mint a brand-new
 * slim session + brand-new WorkflowSession + brand-new
 * AgentSession, wiping all prior context).
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  reduceRuntimeEvent,
  type PendingConfirmation,
} from './sseClient'
import type { RuntimeEvent } from '../types/workflow'

const nextId = () => 'm-test'

// ─────────────────────────────────────────────────────────────────
// sessionId preservation — the multi-turn context fix
// ─────────────────────────────────────────────────────────────────

test('completed event does NOT include sessionId in patches', () => {
  // The fix : `completed` used to emit `sessionId: null`
  // which the consumer (`feedRuntimeEvent`'s `'sessionId' in patches`
  // check) would forward to `setSessionId(null)` — wiping the
  // conversation identity at the worst possible time. The reducer
  // must now return no `sessionId` field at all on `completed`,
  // so the consumer's `'in'` check skips the call and the prior
  // sessionId survives.
  const ev: RuntimeEvent = {
    type: 'completed',
    output: 'Here are 10 substations...',
  }
  const patches = reduceRuntimeEvent(ev, nextId)
  assert.ok(
    !('sessionId' in patches),
    `completed must not emit a sessionId patch; got: ${JSON.stringify(patches)}`,
  )
  // pendingConfirmation IS cleared (the contract says so) — and
  // appended messages include the completed bubble.
  assert.ok('pendingConfirmation' in patches)
  assert.equal(patches.pendingConfirmation, null)
  assert.equal(patches.appendMessages?.length, 1)
  assert.equal(patches.appendMessages?.[0].kind, 'completed')
})

test('start event does NOT touch sessionId (it lives on the SSE header)', () => {
  // Sanity check that the sessionId path is NOT mixed into the
  // reducer's `start` event — the sessionId actually flows through
  // the SSE response `X-Session-Id` header (see
  // `chatActions.dispatchSend` line 82: `if (sessionId)
  // run.setSessionId(sessionId)`), not through the reducer. The
  // reducer must NOT redundantly emit sessionId on `start` — that
  // would overwrite the header value (if both happened to arrive
  // out of order) or simply be dead code.
  const ev: RuntimeEvent = {
    type: 'start',
    session_id: 'run-abc123',
  }
  const patches = reduceRuntimeEvent(ev, nextId)
  assert.ok(
    !('sessionId' in patches),
    `start event must not emit a sessionId patch; got: ${JSON.stringify(patches)}`,
  )
})

test('error event does NOT clear sessionId', () => {
  // Errors are mid-stream events — the session must survive so the
  // user can retry within the same conversation.
  const ev: RuntimeEvent = {
    type: 'error',
    message: 'tool call failed',
  }
  const patches = reduceRuntimeEvent(ev, nextId)
  assert.ok(!('sessionId' in patches))
  // Error IS surfaced.
  assert.equal(patches.error, 'tool call failed')
})

test('confirmation event does NOT clear sessionId', () => {
  // HITL pauses the stream — the session id must persist so the
  // user's reply (sent via /continue) routes back to the same
  // agno WorkflowSession.
  const ev: RuntimeEvent = {
    type: 'confirmation',
    kind: 'ask',
    prompt: 'confirm dispatch?',
    choices: ['approve', 'revise'],
  }
  const patches = reduceRuntimeEvent(ev, nextId)
  assert.ok(!('sessionId' in patches))
  // pendingConfirmation IS set.
  assert.ok(patches.pendingConfirmation)
  assert.equal((patches.pendingConfirmation as PendingConfirmation).kind, 'ask')
})

test('text / tool_call / tool_result events do NOT touch sessionId', () => {
  // Mid-stream events must leave sessionId alone.
  for (const ev of [
    { type: 'text', content: 'hello' } as RuntimeEvent,
    { type: 'tool_call', tool: 'q', args: { x: 1 } } as RuntimeEvent,
    { type: 'tool_result', tool: 'q', ok: true, result: 'ok' } as RuntimeEvent,
  ]) {
    const patches = reduceRuntimeEvent(ev, nextId)
    assert.ok(
      !('sessionId' in patches),
      `${ev.type} must not touch sessionId; got: ${JSON.stringify(patches)}`,
    )
  }
})