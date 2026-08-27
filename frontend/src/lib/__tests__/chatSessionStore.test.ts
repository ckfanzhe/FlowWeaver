/**
 * Unit tests for `chatSessionStore` — the IndexedDB wrapper backing
 * the chat-sidebar transcript persistence (Run + Build modes).
 *
 * Mirrors `snapshotStore.test.ts`'s setup: `fake-indexeddb/auto`
 * polyfills `globalThis.indexedDB` so `idb` can talk to an in-memory
 * DB inside Node. Per-test isolation is handled by `test.beforeEach`
 * deleting the DB so each one starts from a clean slate.
 *
 * Run:  npx tsx --test src/lib/__tests__/chatSessionStore.test.ts
 */
import test from 'node:test'
import assert from 'node:assert/strict'

// IMPORTANT: import the polyfill BEFORE importing the module under
// test. `idb.openDB` calls `indexedDB.open` at construction time; if
// the polyfill isn't installed yet, the `typeof indexedDB === 'undefined'`
// guard inside chatSessionStore returns null and every test below
// silently no-ops.
import 'fake-indexeddb/auto'

import {
  chatSessionKey,
  putRunChat,
  getRunChat,
  putBuildChat,
  getBuildChat,
  deleteRunChat,
  deleteBuildChat,
  deleteUserChats,
  type RunChatEnvelope,
  type BuildChatEnvelope,
} from '../chatSessionStore'

function makeRun(overrides: Partial<RunChatEnvelope> = {}): RunChatEnvelope {
  return {
    key: 'user-A::wf-1',
    messages: [],
    sessionId: null,
    pendingConfirmation: null,
    savedAt: 1700000000000,
    ...overrides,
  }
}

function makeBuild(overrides: Partial<BuildChatEnvelope> = {}): BuildChatEnvelope {
  return {
    key: 'user-A::wf-1',
    messages: [],
    diff: null,
    finished: false,
    selectedPresetId: null,
    savedAt: 1700000000000,
    ...overrides,
  }
}

test.beforeEach(async () => {
  // Wipe the DB between tests so each one starts from a clean slate.
  // fake-indexeddb supports `deleteDatabase`; the per-call open in
  // chatSessionStore means the next test sees a fresh store.
  await new Promise<void>((resolve) => {
    const req = indexedDB.deleteDatabase('agnobuilder-chat-sessions')
    req.onsuccess = req.onerror = req.onblocked = () => resolve()
  })
})

// fake-indexeddb's open IDB connection keeps the Node event loop
// alive past the last test. `deleteDatabase` is the only public
// hook that forces all connections on the named DB to close so
// the test runner can exit cleanly. Same pattern as
// `snapshotStore.test.ts` and `workflowStore.snapshot.test.ts`.
test.after(async () => {
  await new Promise<void>((resolve) => {
    const req = indexedDB.deleteDatabase('agnobuilder-chat-sessions')
    req.onsuccess = req.onerror = req.onblocked = () => resolve()
  })
  setTimeout(() => process.exit(0), 50).unref()
})

// ─────────────────────────────────────────────────────────────────
// Keying
// ─────────────────────────────────────────────────────────────────
test('chatSessionKey: composes userId and workflowId', () => {
  assert.equal(chatSessionKey('alice', 'wf-1'), 'alice::wf-1')
  assert.equal(chatSessionKey('alice', null), 'alice::draft')
  assert.equal(chatSessionKey('', 'wf-1'), '::wf-1')  // degenerate, but defined
})

// ─────────────────────────────────────────────────────────────────
// Round-trip — Run store
// ─────────────────────────────────────────────────────────────────
test('putRunChat → getRunChat round-trips envelope verbatim', async () => {
  const env = makeRun({
    sessionId: 'sess-123',
    savedAt: 1234567890,
  })
  await putRunChat(env)
  const got = await getRunChat(env.key)
  assert.ok(got, 'expected envelope to be readable')
  assert.equal(got!.key, env.key)
  assert.equal(got!.sessionId, 'sess-123')
  assert.equal(got!.savedAt, 1234567890)
})

test('getRunChat: returns undefined for unknown key', async () => {
  const got = await getRunChat('never-written::wf-x')
  assert.equal(got, undefined)
})

test('putRunChat: second put with same key upserts (replaces)', async () => {
  const env1 = makeRun({ sessionId: 'first' })
  const env2 = makeRun({ sessionId: 'second', savedAt: env1.savedAt + 1 })
  await putRunChat(env1)
  await putRunChat(env2)
  const got = await getRunChat(env1.key)
  assert.equal(got!.sessionId, 'second')
})

test('deleteRunChat: removes entry', async () => {
  const env = makeRun()
  await putRunChat(env)
  await deleteRunChat(env.key)
  const got = await getRunChat(env.key)
  assert.equal(got, undefined)
})

test('deleteRunChat: silent on missing key (no throw)', async () => {
  await deleteRunChat('never-written::wf-y')
  // (no assert — reaching here without throwing is the test)
})

// ─────────────────────────────────────────────────────────────────
// Round-trip — Build store
// ─────────────────────────────────────────────────────────────────
test('putBuildChat → getBuildChat round-trips envelope verbatim', async () => {
  const env = makeBuild({
    finished: true,
    selectedPresetId: 'preset-xyz',
    savedAt: 9876543210,
  })
  await putBuildChat(env)
  const got = await getBuildChat(env.key)
  assert.ok(got, 'expected envelope to be readable')
  assert.equal(got!.key, env.key)
  assert.equal(got!.finished, true)
  assert.equal(got!.selectedPresetId, 'preset-xyz')
  assert.equal(got!.savedAt, 9876543210)
})

test('getBuildChat: returns undefined for unknown key', async () => {
  const got = await getBuildChat('never-written::wf-x')
  assert.equal(got, undefined)
})

test('deleteBuildChat: removes entry', async () => {
  const env = makeBuild()
  await putBuildChat(env)
  await deleteBuildChat(env.key)
  const got = await getBuildChat(env.key)
  assert.equal(got, undefined)
})

// ─────────────────────────────────────────────────────────────────
// Isolation — Run and Build use different stores
// ─────────────────────────────────────────────────────────────────
test('Run and Build stores are independent (same key, different envelopes)', async () => {
  const key = 'alice::wf-1'
  await putRunChat(makeRun({ key, sessionId: 'run-sess' }))
  await putBuildChat(makeBuild({ key, selectedPresetId: 'b-preset' }))
  const runView = await getRunChat(key)
  const buildView = await getBuildChat(key)
  assert.equal(runView!.sessionId, 'run-sess')
  assert.equal(buildView!.selectedPresetId, 'b-preset')
})

// ─────────────────────────────────────────────────────────────────
// Namespacing — user A's data is invisible to user B
// ─────────────────────────────────────────────────────────────────
test('namespacing by userId: user B cannot read user A chat', async () => {
  const aRun = makeRun({ key: 'alice::wf-1', sessionId: 'alice-sess' })
  const bRun = makeRun({ key: 'bob::wf-1', sessionId: 'bob-sess' })
  await putRunChat(aRun)
  await putRunChat(bRun)
  const aliceView = await getRunChat('alice::wf-1')
  const bobView = await getRunChat('bob::wf-1')
  assert.equal(aliceView!.sessionId, 'alice-sess')
  assert.equal(bobView!.sessionId, 'bob-sess')
  // Deleting alice's run must NOT touch bob's.
  await deleteRunChat('alice::wf-1')
  assert.equal(await getRunChat('alice::wf-1'), undefined)
  assert.equal((await getRunChat('bob::wf-1'))!.sessionId, 'bob-sess')
})

// ─────────────────────────────────────────────────────────────────
// Multi-workflow — same user, multiple workflows
// ─────────────────────────────────────────────────────────────────
test('envelope with workflowId=null uses draft namespace', async () => {
  const draftEnv = makeBuild({
    key: 'alice::draft',
    selectedPresetId: 'draft-preset',
  })
  await putBuildChat(draftEnv)
  const got = await getBuildChat(chatSessionKey('alice', null))
  assert.equal(got!.selectedPresetId, 'draft-preset')
})

test('per-workflow isolation: same user, two workflows', async () => {
  const wf1Env = makeRun({ key: 'alice::wf-1', sessionId: 'sess-1' })
  const wf2Env = makeRun({ key: 'alice::wf-2', sessionId: 'sess-2' })
  await putRunChat(wf1Env)
  await putRunChat(wf2Env)
  const wf1View = await getRunChat('alice::wf-1')
  const wf2View = await getRunChat('alice::wf-2')
  assert.equal(wf1View!.sessionId, 'sess-1')
  assert.equal(wf2View!.sessionId, 'sess-2')
})

// ─────────────────────────────────────────────────────────────────
// deleteUserChats — signOut semantics
// ─────────────────────────────────────────────────────────────────
test('deleteUserChats: wipes every entry for the user across both stores', async () => {
  await putRunChat(makeRun({ key: 'alice::wf-1', sessionId: 's1' }))
  await putRunChat(makeRun({ key: 'alice::wf-2', sessionId: 's2' }))
  await putBuildChat(makeBuild({ key: 'alice::wf-1', selectedPresetId: 'p1' }))
  await putBuildChat(makeBuild({ key: 'alice::draft', selectedPresetId: 'pd' }))
  await putRunChat(makeRun({ key: 'bob::wf-1', sessionId: 'bob-s' }))

  await deleteUserChats('alice')

  // Every alice entry is gone, including the draft.
  assert.equal(await getRunChat('alice::wf-1'), undefined)
  assert.equal(await getRunChat('alice::wf-2'), undefined)
  assert.equal(await getBuildChat('alice::wf-1'), undefined)
  assert.equal(await getBuildChat('alice::draft'), undefined)
  // Bob is untouched.
  assert.equal((await getRunChat('bob::wf-1'))!.sessionId, 'bob-s')
})

test('deleteUserChats: silent on empty user (no entries to wipe)', async () => {
  await deleteUserChats('nobody')
  // (no assert — reaching here without throwing is the test)
})

test('deleteUserChats: empty userId is a no-op (does not wipe other users)', async () => {
  await putRunChat(makeRun({ key: 'alice::wf-1', sessionId: 's1' }))
  await deleteUserChats('')
  assert.equal((await getRunChat('alice::wf-1'))!.sessionId, 's1')
})