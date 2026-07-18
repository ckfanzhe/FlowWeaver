/**
 * Tests for the chat-builder SSE reducer.
 *
 * Run:  npx tsx --test src/store/builderChatSseClient.test.ts
 *
 * Mirrors `chatStore.test.ts` (the runtime SSE reducer). The
 * builder reducer is a pure function — feed events in, assert
 * the returned patches.
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import { reduceBuilderEvent } from './builderChatSseClient'
import type { BuilderEvent } from '../types/chatBuilder'

const idCounter = { v: 0 }
const nextId = () => `m-${++idCounter.v}`

test('start sets sessionId (no bubble; framework tracks running state)', () => {
  const ev: BuilderEvent = { type: 'start', session_id: 'chat-abc' }
  const patches = reduceBuilderEvent(ev, nextId)
  assert.equal(patches.sessionId, 'chat-abc')
  // No appendMessages — the assistant message's `running` status
  // (set by the orchestrator before the stream opens) is the
  // thinking indicator. The framework renders it natively.
  assert.equal(patches.appendMessages, undefined)
})

test('text pushes a text bubble', () => {
  const ev: BuilderEvent = { type: 'text', content: 'Adding a router.' }
  const patches = reduceBuilderEvent(ev, nextId)
  assert.equal(patches.appendMessages?.length, 1)
  assert.equal(patches.appendMessages?.[0].kind, 'text')
  assert.equal(
    (patches.appendMessages?.[0].data as { content: string }).content,
    'Adding a router.',
  )
})

test('text with delta=false (or absent) still pushes a text bubble', () => {
  // The batched fallback path emits `text` events without
  // `delta`. Those should keep behaving like the old reducer —
  // one new bubble per event. The streaming path uses
  // `delta=true` (test below).
  for (const ev of [
    { type: 'text', content: 'plain.' } as BuilderEvent,
    { type: 'text', content: 'explicit-false.', delta: false } as BuilderEvent,
  ]) {
    const patches = reduceBuilderEvent(ev, nextId)
    assert.equal(patches.appendToLastText, undefined)
    assert.equal(patches.appendMessages?.length, 1)
    assert.equal(patches.appendMessages?.[0].kind, 'text')
  }
})

test('text with delta=true emits appendToLastText (no new bubble)', () => {
  // Streaming fragment — the orchestrator will APPEND `content`
  // to the last text bubble instead of starting a new one.
  // This is how the chat shows the LLM "typing" token by token
  // instead of buffering and dumping all text at once.
  const ev: BuilderEvent = {
    type: 'text',
    content: 'def quicksort(arr):\n  ',
    delta: true,
  }
  const patches = reduceBuilderEvent(ev, nextId)
  assert.equal(patches.appendMessages, undefined)
  assert.deepEqual(patches.appendToLastText, { content: ev.content })
})

test('multiple consecutive text deltas each emit appendToLastText', () => {
  // Three fragments of a streaming response — each one is a
  // patch to the same bubble, not three separate bubbles.
  const fragments = [
    { type: 'text', content: 'def ', delta: true } as BuilderEvent,
    { type: 'text', content: 'quicksort', delta: true } as BuilderEvent,
    { type: 'text', content: '(arr):\n  pass', delta: true } as BuilderEvent,
  ]
  for (const ev of fragments) {
    const patches = reduceBuilderEvent(ev, nextId)
    assert.equal(patches.appendMessages, undefined)
    assert.deepEqual(patches.appendToLastText, {
      content: (ev as { content: string }).content,
    })
  }
})

test('tool_call pushes a tool_call bubble', () => {
  const ev: BuilderEvent = {
    type: 'tool_call',
    tool_call_id: 'c1',
    tool: 'add_node',
    args: { type: 'agent' },
  }
  const patches = reduceBuilderEvent(ev, nextId)
  assert.equal(patches.appendMessages?.[0].kind, 'tool_call')
  assert.equal(
    (patches.appendMessages?.[0].data as { tool: string }).tool,
    'add_node',
  )
})

test('tool_result pushes a tool_result bubble', () => {
  const ev: BuilderEvent = {
    type: 'tool_result',
    tool_call_id: 'c1',
    tool: 'add_node',
    ok: true,
    message: 'OK',
  }
  const patches = reduceBuilderEvent(ev, nextId)
  assert.equal(patches.appendMessages?.[0].kind, 'tool_result')
  assert.equal((patches.appendMessages?.[0].data as { ok: boolean }).ok, true)
})

test('diff replaces the diff and pushes a diff bubble', () => {
  const ev: BuilderEvent = {
    type: 'diff',
    summary: { added_nodes: 1, removed_nodes: 0, updated_nodes: 0, added_edges: 0, removed_edges: 0 },
    nodes: [{ op: 'added', node: { id: 'a2' } }],
    edges: [],
  }
  const patches = reduceBuilderEvent(ev, nextId)
  assert.equal(patches.diff?.summary.added_nodes, 1)
  assert.equal(patches.appendMessages?.[0].kind, 'diff')
})

test('completed marks finished and pushes a completed bubble', () => {
  const ev: BuilderEvent = { type: 'completed', output: '' }
  const patches = reduceBuilderEvent(ev, nextId)
  assert.equal(patches.finished, true)
  assert.equal(patches.appendMessages?.[0].kind, 'completed')
})

test('error marks finished and pushes an error bubble', () => {
  const ev: BuilderEvent = { type: 'error', message: 'LLM call failed' }
  const patches = reduceBuilderEvent(ev, nextId)
  assert.equal(patches.finished, true)
  assert.equal(patches.error, 'LLM call failed')
  assert.equal(patches.appendMessages?.[0].kind, 'error')
})

test('thinking pushes a thinking bubble (no sessionId change)', () => {
  const ev: BuilderEvent = { type: 'thinking' }
  const patches = reduceBuilderEvent(ev, nextId)
  assert.equal(patches.appendMessages?.[0].kind, 'thinking')
  assert.equal(patches.sessionId, undefined)
})
