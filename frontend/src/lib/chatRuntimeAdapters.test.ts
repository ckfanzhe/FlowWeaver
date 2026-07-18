/**
 * Tests for the chat runtime adapters.
 *
 * Run:  npx tsx --test src/lib/chatRuntimeAdapters.test.ts
 *
 * The adapters are pure functions that map our `ChatMessage` /
 * `BuilderChatMessage` shapes into `ThreadMessageLike`. They're
 * the bridge between our zustand stores and assistant-ui's
 * `ExternalStoreAdapter`. The converters don't need React or
 * zustand — we can drive them directly with sample data.
 *
 *  — adapter now emits native assistant-ui parts
 * (text / tool-call / reasoning) where one exists. The
 * framework's `ToolGroup` slot auto-wraps consecutive tool-call
 * parts and the framework's `Reasoning:` slot handles thinking.
 * These tests assert the native shapes, not the old data-part
 * workarounds.
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  convertRuntimeMessage,
  convertBuilderMessage,
} from './chatRuntimeAdapters'

// ───────────────────────────────────────────────────────────────
// Runtime converter
// ───────────────────────────────────────────────────────────────

test('runtime: user message → user role with text part', () => {
  const out = convertRuntimeMessage(
    { id: 'm1', kind: 'user', data: { text: 'hello' } },
    0,
  )
  assert.equal(out.role, 'user')
  assert.equal(out.id, 'm1')
  assert.ok(Array.isArray(out.content))
  const parts = out.content as Array<{ type: string; text?: string }>
  assert.equal(parts[0].type, 'text')
  assert.equal(parts[0].text, 'hello')
})

test('runtime: text message → assistant text part', () => {
  const out = convertRuntimeMessage(
    { id: 'm2', kind: 'text', data: { content: 'hi back' } },
    0,
  )
  assert.equal(out.role, 'assistant')
  const parts = out.content as Array<{ type: string; text?: string }>
  assert.equal(parts[0].type, 'text')
  assert.equal(parts[0].text, 'hi back')
})

test('runtime: tool_call → native tool-call part with toolName+args', () => {
  const out = convertRuntimeMessage(
    {
      id: 'm3',
      kind: 'tool_call',
      data: { tool: 'add_node', args: { type: 'agent' }, tool_call_id: 'c1' },
    },
    0,
  )
  const parts = out.content as Array<{
    type: string
    toolName?: string
    args?: Record<string, unknown>
    toolCallId?: string
  }>
  assert.equal(parts[0].type, 'tool-call')
  assert.equal(parts[0].toolName, 'add_node')
  assert.deepEqual(parts[0].args, { type: 'agent' })
  assert.equal(parts[0].toolCallId, 'c1')
})

test('runtime: tool_result → native tool-call part with result set', () => {
  const out = convertRuntimeMessage(
    {
      id: 'm4',
      kind: 'tool_result',
      data: { tool: 'add_node', result: { id: 'a1' }, ok: true, tool_call_id: 'c1' },
    },
    0,
  )
  const parts = out.content as Array<{
    type: string
    toolName?: string
    result?: unknown
    isError?: boolean
  }>
  assert.equal(parts[0].type, 'tool-call')
  assert.equal(parts[0].toolName, 'add_node')
  assert.deepEqual(parts[0].result, { id: 'a1' })
  assert.equal(parts[0].isError, false)
})

test('runtime: confirmation → data part with kind+prompt', () => {
  const out = convertRuntimeMessage(
    {
      id: 'm5',
      kind: 'confirmation',
      data: { kind: 'ask', prompt: 'Continue?', choices: ['yes'] },
    },
    0,
  )
  const parts = out.content as Array<{
    type: string
    name?: string
    data?: { kind?: string; prompt?: string; choices?: string[] }
  }>
  assert.equal(parts[0].type, 'data')
  assert.equal(parts[0].name, 'confirmation')
  assert.equal(parts[0].data?.prompt, 'Continue?')
  assert.deepEqual(parts[0].data?.choices, ['yes'])
})

test('runtime: completed → status=complete + completed data part', () => {
  const out = convertRuntimeMessage(
    { id: 'm6', kind: 'completed', data: { text: 'Done.' } },
    0,
  )
  assert.equal(out.role, 'assistant')
  assert.deepEqual(out.status, { type: 'complete', reason: 'stop' })
  const parts = out.content as Array<{ type: string; name?: string }>
  assert.equal(parts[0].name, 'completed')
})

test('runtime: error → status=incomplete + error data part', () => {
  const out = convertRuntimeMessage(
    { id: 'm7', kind: 'error', data: { message: 'boom' } },
    0,
  )
  assert.deepEqual(out.status, { type: 'incomplete', reason: 'error' })
  const parts = out.content as Array<{ name?: string; data?: { message?: string } }>
  assert.equal(parts[0].name, 'error')
  assert.equal(parts[0].data?.message, 'boom')
})

test('runtime: missing id falls back to index', () => {
  const out = convertRuntimeMessage(
    { id: '', kind: 'user', data: { text: 'x' } },
    7,
  )
  assert.equal(out.id, 'm-7')
})

test('runtime: assistant text gets leading bullet stripped', () => {
  const out = convertRuntimeMessage(
    {
      id: 'm8',
      kind: 'text',
      data: { content: '●\n\nThe workflow has been updated.' },
    },
    0,
  )
  const parts = out.content as Array<{ text?: string }>
  assert.equal(parts[0].text, 'The workflow has been updated.')
})

// ───────────────────────────────────────────────────────────────
// Builder converter
// ───────────────────────────────────────────────────────────────

test('builder: thinking → status=running + native reasoning part', () => {
  const out = convertBuilderMessage(
    { id: 'b1', kind: 'thinking', data: {} },
    0,
  )
  assert.deepEqual(out.status, { type: 'running' })
  const parts = out.content as Array<{ type: string }>
  assert.equal(parts[0].type, 'reasoning')
})

test('builder: tool_call → native tool-call part with toolName+args', () => {
  const out = convertBuilderMessage(
    {
      id: 'b2',
      kind: 'tool_call',
      data: { tool: 'add_node', args: { type: 'agent' }, tool_call_id: 'c1' },
    },
    0,
  )
  const parts = out.content as Array<{
    type: string
    toolName?: string
    args?: Record<string, unknown>
    toolCallId?: string
  }>
  assert.equal(parts[0].type, 'tool-call')
  assert.equal(parts[0].toolName, 'add_node')
  assert.deepEqual(parts[0].args, { type: 'agent' })
  assert.equal(parts[0].toolCallId, 'c1')
})

test('builder: tool_result with ok=false surfaces isError', () => {
  const out = convertBuilderMessage(
    {
      id: 'b3',
      kind: 'tool_result',
      data: { tool: 'add_node', ok: false, message: 'rejected', tool_call_id: 'c1' },
    },
    0,
  )
  const parts = out.content as Array<{
    type: string
    toolName?: string
    isError?: boolean
    result?: unknown
  }>
  assert.equal(parts[0].type, 'tool-call')
  assert.equal(parts[0].isError, true)
  assert.equal(parts[0].result, 'rejected')
})

test('runtime: tool_result without ok defaults to success (not error)', () => {
  // The  dispatch_task export regression: the runtime
  // emitted `tool_result` events with no `ok` field, and the
  // adapter's `?? false` default made every successful call show
  // ✗. The runtime now populates `ok` explicitly, but if a legacy
  // event slips through (or a tool payload itself carries
  // `success: true`), the default should NOT be error.
  const out = convertRuntimeMessage(
    {
      id: 'm5',
      kind: 'tool_result',
      data: {
        tool: 'dispatch_task',
        result: { success: true, task_id: 'T-1' },
        tool_call_id: 'c1',
      },
    },
    0,
  )
  const parts = out.content as Array<{
    type: string
    toolName?: string
    isError?: boolean
    result?: unknown
  }>
  assert.equal(parts[0].isError, false)
  assert.deepEqual(parts[0].result, { success: true, task_id: 'T-1' })
})

test('runtime: tool_result with result.success=false surfaces isError', () => {
  // Wire-drift safety: if the backend hasn't been updated to forward
  // `ok` yet, but the tool payload itself reports `success: false`,
  // the adapter should still render ✗.
  const out = convertRuntimeMessage(
    {
      id: 'm6',
      kind: 'tool_result',
      data: {
        tool: 'dispatch_task',
        result: { success: false, error: 'city not found' },
        tool_call_id: 'c1',
      },
    },
    0,
  )
  const parts = out.content as Array<{
    type: string
    isError?: boolean
    result?: unknown
  }>
  assert.equal(parts[0].isError, true)
})

test('runtime: tool_result with explicit ok=false overrides payload success', () => {
  // When BOTH `ok` and `result.success` are present, `ok` wins
  // (the runtime's authoritative signal).
  const out = convertRuntimeMessage(
    {
      id: 'm7',
      kind: 'tool_result',
      data: {
        tool: 'dispatch_task',
        result: { success: true, task_id: 'T-1' },
        ok: false,
        tool_call_id: 'c1',
      },
    },
    0,
  )
  const parts = out.content as Array<{
    type: string
    isError?: boolean
  }>
  assert.equal(parts[0].isError, true)
})

test('builder: diff → data part with full diff payload', () => {
  const out = convertBuilderMessage(
    {
      id: 'b4',
      kind: 'diff',
      data: {
        summary: { added_nodes: 1, removed_nodes: 0, updated_nodes: 0, added_edges: 0, removed_edges: 0 },
        nodes: [{ op: 'added', node: { id: 'a1', type: 'agent' } }],
        edges: [],
      },
    },
    0,
  )
  const parts = out.content as Array<{
    name?: string
    data?: { summary?: Record<string, number>; nodes?: unknown[] }
  }>
  assert.equal(parts[0].name, 'diff')
  assert.equal(parts[0].data?.summary?.added_nodes, 1)
  assert.equal(parts[0].data?.nodes?.length, 1)
})

test('builder: error → status=incomplete', () => {
  const out = convertBuilderMessage(
    { id: 'b5', kind: 'error', data: { message: 'LLM failed' } },
    0,
  )
  assert.deepEqual(out.status, { type: 'incomplete', reason: 'error' })
  const parts = out.content as Array<{ name?: string }>
  assert.equal(parts[0].name, 'error')
})

test('builder: completed → status=complete', () => {
  const out = convertBuilderMessage(
    { id: 'b6', kind: 'completed', data: { text: 'Applied.' } },
    0,
  )
  assert.deepEqual(out.status, { type: 'complete', reason: 'stop' })
  const parts = out.content as Array<{ name?: string }>
  assert.equal(parts[0].name, 'completed')
})

test('builder: text → assistant text part', () => {
  const out = convertBuilderMessage(
    { id: 'b7', kind: 'text', data: { content: 'Added 1 node.' } },
    0,
  )
  const parts = out.content as Array<{ type: string; text?: string }>
  assert.equal(parts[0].type, 'text')
  assert.equal(parts[0].text, 'Added 1 node.')
})

test('builder: user → user role with text part', () => {
  const out = convertBuilderMessage(
    { id: 'b8', kind: 'user', data: { text: 'add a router' } },
    0,
  )
  assert.equal(out.role, 'user')
  const parts = out.content as Array<{ type: string; text?: string }>
  assert.equal(parts[0].text, 'add a router')
})
