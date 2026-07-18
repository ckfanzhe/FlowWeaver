/**
 * Tests for the chat-builder store's `appendToLastText` action.
 *
 * The action drives the "LLM is typing" feel: each streaming
 * `text` event with `delta=true` extends the existing text bubble
 * instead of opening a new one. Without this, every fragment
 * would create its own bubble and the chat would flicker
 * ("H", "Hello", "Hello world").
 *
 * Run:  npx tsx --test src/store/builderChatStore.test.ts
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import { useBuilderChatStore } from './builderChatStore'

function reset() {
  useBuilderChatStore.getState().reset()
}

test('appendToLastText on empty store creates a text bubble', () => {
  reset()
  useBuilderChatStore.getState().appendToLastText('Hello')
  const msgs = useBuilderChatStore.getState().messages
  assert.equal(msgs.length, 1)
  assert.equal(msgs[0].kind, 'text')
  assert.equal((msgs[0].data as { content: string }).content, 'Hello')
})

test('appendToLastText appends to the LAST text bubble in place', () => {
  reset()
  const store = useBuilderChatStore.getState()
  store.appendMessages([
    { id: 'm1', kind: 'user', data: { text: 'Write quicksort' } },
    { id: 'm2', kind: 'thinking', data: {} },
    { id: 'm3', kind: 'text', data: { content: 'def ', delta: true } },
    { id: 'm4', kind: 'tool_call', data: {} },
    { id: 'm5', kind: 'tool_result', data: {} },
    { id: 'm6', kind: 'text', data: { content: 'Here: ', delta: true } },
  ])
  // The last text bubble is m6 — appending should extend m6,
      // not m3 (earlier text bubble), not create m7.
  store.appendToLastText('```python\n')
  store.appendToLastText('pass\n')
  store.appendToLastText('```')

  const msgs = useBuilderChatStore.getState().messages
  assert.equal(msgs.length, 6, 'no new bubble should be created')
  assert.equal((msgs[2].data as { content: string }).content, 'def ')
  assert.equal(
    (msgs[5].data as { content: string }).content,
    'Here: ```python\npass\n```',
  )
})

test('appendToLastText does NOT bridge across non-text messages', () => {
  // After the LLM produces a tool call, then a tool result,
  // then resumes talking, the first new text delta has no
  // preceding text bubble (the last bubble is `tool_result`,
  // not `text`) — so it should create a NEW bubble rather
  // than wrongly extending the earlier text bubble across
  // the gap.
  reset()
  const store = useBuilderChatStore.getState()
  store.appendMessages([
    { id: 'm1', kind: 'text', data: { content: 'First reply.' } },
    { id: 'm2', kind: 'tool_call', data: {} },
    { id: 'm3', kind: 'tool_result', data: {} },
  ])
  store.appendToLastText('Second reply fragment 1. ')
  store.appendToLastText('Fragment 2.')

  const msgs = useBuilderChatStore.getState().messages
  // 3 original + 1 new text bubble = 4 total. The two
  // consecutive deltas extend the new bubble, not create
  // two more.
  assert.equal(msgs.length, 4)
  // The original text bubble is untouched.
  assert.equal((msgs[0].data as { content: string }).content, 'First reply.')
  // The new bubble accumulates both fragments.
  assert.equal(msgs[3].kind, 'text')
  assert.equal(
    (msgs[3].data as { content: string }).content,
    'Second reply fragment 1. Fragment 2.',
  )
})