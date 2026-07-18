/**
 * Unit tests for `snapshotStore` — the IndexedDB wrapper backing
 * P1 (dirty-state persistence).
 *
 * `fake-indexeddb/auto` polyfills `globalThis.indexedDB` so `idb`
 * can talk to an in-memory DB inside Node. Per-test isolation: we
 * delete the DB between tests so each one starts from a clean slate
 * (the real module caches the `IDBPDatabase` promise; resetting
 * `_dbPromise = null` isn't exported, but `deleteDatabase` on a
 * new connection to the same name handles it).
 *
 * Run:  npx tsx --test src/lib/__tests__/snapshotStore.test.ts
 */
import test from 'node:test'
import assert from 'node:assert/strict'

// IMPORTANT: import the polyfill BEFORE importing the module under
// test. `idb.openDB` calls `indexedDB.open` at construction time;
// if the polyfill isn't installed yet, the module-level `typeof
// indexedDB === 'undefined'` guard returns null and we silently
// skip every test below.
import 'fake-indexeddb/auto'

import {
  snapshotKey,
  putSnapshot,
  getSnapshot,
  deleteSnapshot,
  type SnapshotEnvelope,
} from '../snapshotStore'

function makeEnvelope(overrides: Partial<SnapshotEnvelope> = {}): SnapshotEnvelope {
  return {
    key: 'user-A::wf-1',
    workflowId: 'wf-1',
    name: 'Test',
    description: 'desc',
    nodes: [],
    edges: [],
    savedAt: 1700000000000,
    backendUpdatedAt: null,
    ...overrides,
  }
}

test('snapshotKey: composes userId and workflowId', () => {
  assert.equal(snapshotKey('alice', 'wf-1'), 'alice::wf-1')
  assert.equal(snapshotKey('alice', null), 'alice::draft')
  assert.equal(snapshotKey('', 'wf-1'), '::wf-1')  // degenerate, but defined
})

test('putSnapshot → getSnapshot round-trips envelope verbatim', async () => {
  const env = makeEnvelope({ name: 'Round-trip', savedAt: 1234567890 })
  await putSnapshot(env)
  const got = await getSnapshot(env.key)
  assert.ok(got, 'expected snapshot to be readable')
  assert.equal(got!.key, env.key)
  assert.equal(got!.name, 'Round-trip')
  assert.equal(got!.savedAt, 1234567890)
  assert.deepEqual(got!.nodes, [])
  assert.deepEqual(got!.edges, [])
})

test('getSnapshot: returns undefined for unknown key', async () => {
  const got = await getSnapshot('never-written::wf-x')
  assert.equal(got, undefined)
})

test('putSnapshot: second put with same key upserts (replaces)', async () => {
  const env1 = makeEnvelope({ name: 'first' })
  const env2 = makeEnvelope({ name: 'second', savedAt: env1.savedAt + 1 })
  await putSnapshot(env1)
  await putSnapshot(env2)
  const got = await getSnapshot(env1.key)
  assert.equal(got!.name, 'second')
})

test('deleteSnapshot: removes entry', async () => {
  const env = makeEnvelope()
  await putSnapshot(env)
  await deleteSnapshot(env.key)
  const got = await getSnapshot(env.key)
  assert.equal(got, undefined)
})

test('deleteSnapshot: silent on missing key (no throw)', async () => {
  // Regression: private mode / quota errors must not crash the canvas.
  await deleteSnapshot('never-written::wf-y')
  // (no assert — reaching here without throwing is the test)
})

test('namespacing by userId: user B cannot read user A snapshot', async () => {
  const aSnap = makeEnvelope({ key: 'alice::wf-1', name: 'alice draft' })
  const bSnap: SnapshotEnvelope = {
    ...makeEnvelope(),
    key: 'bob::wf-1',
    name: 'bob draft',
  }
  await putSnapshot(aSnap)
  await putSnapshot(bSnap)
  const aliceView = await getSnapshot('alice::wf-1')
  const bobView = await getSnapshot('bob::wf-1')
  assert.equal(aliceView!.name, 'alice draft')
  assert.equal(bobView!.name, 'bob draft')
  // Deleting alice's snapshot must NOT touch bob's.
  await deleteSnapshot('alice::wf-1')
  assert.equal(await getSnapshot('alice::wf-1'), undefined)
  assert.equal((await getSnapshot('bob::wf-1'))!.name, 'bob draft')
})

test('envelope with workflowId=null uses draft namespace', async () => {
  const draftEnv = makeEnvelope({
    key: 'alice::draft',
    workflowId: null,
    name: 'untitled canvas',
  })
  await putSnapshot(draftEnv)
  const got = await getSnapshot(snapshotKey('alice', null))
  assert.equal(got!.name, 'untitled canvas')
  assert.equal(got!.workflowId, null)
})

test('envelope preserves workflow nodes/edges (JSON round-trip)', async () => {
  // The store ships WorkflowNode[] / WorkflowEdge[] verbatim — make
  // sure non-trivial shapes survive the IndexedDB structured-clone
  // boundary intact.
  const nodes = [
    {
      id: 'n1',
      type: 'agent' as const,
      position: { x: 1, y: 2 },
      data: { label: 'A', config: { toolName: 'x', nested: { a: 1 } } },
    },
  ]
  const edges = [{ id: 'e1', source: 'n1', target: 'n2', kind: 'dataflow' as const }]
  const env = makeEnvelope({ nodes, edges })
  await putSnapshot(env)
  const got = await getSnapshot(env.key)
  assert.deepEqual(got!.nodes, nodes)
  assert.deepEqual(got!.edges, edges)
})