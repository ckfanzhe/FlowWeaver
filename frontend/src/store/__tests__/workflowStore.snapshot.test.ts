/**
 * Integration tests for the snapshot subscribe / actions wired into
 * `workflowStore`. Verifies:
 *
 *   1. `applySnapshot` projects an IndexedDB envelope to the canvas
 *      and marks it dirty.
 *   2. `recordSnapshot` writes the current dirty state to IndexedDB
 *      under the (userId, workflowId) composite key.
 *   3. The snapshot subscribe fires `recordSnapshot` on every
 *      content-changing mutation while `dirty === true`.
 *   4. After `save()` succeeds (dirty flips to false) the snapshot
 *      is removed so the next boot doesn't offer a stale restore.
 *   5. Cross-user isolation: a snapshot under user A's namespace is
 *      invisible to user B (different key).
 *
 * Run:  npx tsx --test src/store/__tests__/workflowStore.snapshot.test.ts
 */
import test from 'node:test'
import assert from 'node:assert/strict'

// Polyfill BEFORE importing the workflowStore — its module-level
// beforeunload listener touches `window`, but the recordSnapshot
// path inside the store doesn't need browser globals at construction.
import 'fake-indexeddb/auto'

import {
  _resetNodeTypesManifestCache,
  fetchNodeTypesManifest,
  type NodeTypesManifest,
} from '../../api/nodeTypes'
import { useWorkflowStore } from '../workflowStore'
import { useIdentityStore } from '../identityStore'
import {
  snapshotKey,
  getSnapshot,
  type SnapshotEnvelope,
} from '../../lib/snapshotStore'

// ─────────────────────────────────────────────────────────────
// Test manifest stub — keeps `nodeTypesManifest()` calls inside
// `addNode` (which loads `defaultConfig`) from blowing up.
// ─────────────────────────────────────────────────────────────
const testManifest: NodeTypesManifest = {
  schemaVersion: 2,
  // Node-type collapse: `parallel`+`steps` → `flow`,
  // `router`+`condition` → `branch`, `http`+`mcp`+`tools` → `tool`.
  types: ['agent', 'branch', 'flow', 'loop',
          'ask', 'tool', 'wikipedia'],
  entries: {
    agent:       { category: 'executable', kind: 'executable', extends: null, displayName: 'x', i18nKey: 'x', color: '', textColor: '', icon: 'AgentIcon', paletteOrder: 0, ui: { group: 'Core', form: 'AgentForm', paletteOrder: 0 }, capabilities: { compoundPass: null, isToolSource: false, needsToolWiring: true, skipPass1: false, stepWrapper: 'agent' }, defaultConfig: {}, io: { inputs: [], outputs: [], tools: [] } },
    branch:      { category: 'compound',   kind: 'compound',   extends: null, displayName: 'x', i18nKey: 'x', color: '', textColor: '', icon: 'AgentIcon', paletteOrder: 0, ui: { group: 'Core', form: 'BranchForm', paletteOrder: 0 }, capabilities: { compoundPass: 20, isToolSource: false, needsToolWiring: false, skipPass1: false, stepWrapper: 'none' }, defaultConfig: {}, io: { inputs: [], outputs: [], tools: [] } },
    flow:        { category: 'compound',   kind: 'compound',   extends: null, displayName: 'x', i18nKey: 'x', color: '', textColor: '', icon: 'AgentIcon', paletteOrder: 0, ui: { group: 'Core', form: 'FlowForm', paletteOrder: 0 }, capabilities: { compoundPass: 10, isToolSource: false, needsToolWiring: false, skipPass1: false, stepWrapper: 'none' }, defaultConfig: {}, io: { inputs: [], outputs: [], tools: [] } },
    loop:        { category: 'compound',   kind: 'compound',   extends: null, displayName: 'x', i18nKey: 'x', color: '', textColor: '', icon: 'AgentIcon', paletteOrder: 0, ui: { group: 'Core', form: 'LoopForm', paletteOrder: 0 }, capabilities: { compoundPass: 30, isToolSource: false, needsToolWiring: false, skipPass1: false, stepWrapper: 'none' }, defaultConfig: {}, io: { inputs: [], outputs: [], tools: [] } },
    ask: { category: 'executable', kind: 'executable', extends: null, displayName: 'x', i18nKey: 'x', color: '', textColor: '', icon: 'AgentIcon', paletteOrder: 0, ui: { group: 'Core', form: 'AskForm', paletteOrder: 0 }, capabilities: { compoundPass: null, isToolSource: false, needsToolWiring: false, skipPass1: false, stepWrapper: 'ask' }, defaultConfig: {}, io: { inputs: [], outputs: [], tools: [] } },
    // Tool-source collapse: http + mcp + tools → single tool entry
    tool:        { category: 'tool_source', kind: 'tool_source', extends: null, displayName: 'x', i18nKey: 'x', color: '', textColor: '', icon: 'AgentIcon', paletteOrder: 0, ui: { group: 'Core', form: 'ToolForm', paletteOrder: 0 }, capabilities: { compoundPass: null, isToolSource: true, needsToolWiring: false, skipPass1: false, stepWrapper: 'none' }, defaultConfig: { source: 'function' }, io: { inputs: [], outputs: [], tools: [] } },
    wikipedia:   { category: 'tool_source', kind: 'tool_source', extends: 'tool', displayName: 'x', i18nKey: 'x', color: '', textColor: '', icon: 'ToolIcon', paletteOrder: 0, ui: { group: 'Core', form: 'ToolForm', paletteOrder: 0 }, capabilities: { compoundPass: null, isToolSource: true, needsToolWiring: false, skipPass1: false, stepWrapper: 'none' }, defaultConfig: {}, io: { inputs: [], outputs: [], tools: [] } },
  },
}

async function primeManifestCache(): Promise<void> {
  _resetNodeTypesManifestCache()
  const originalFetch = globalThis.fetch
  globalThis.fetch = (async () =>
    new Response(JSON.stringify(testManifest), { status: 200 })) as typeof fetch
  try {
    await fetchNodeTypesManifest()
  } finally {
    globalThis.fetch = originalFetch
  }
}

function resetStores(): void {
  useWorkflowStore.getState().reset()
  // Clear localStorage so the workflowStore's persist subscribe
  // doesn't observe a stale `lastWorkflowId` between tests.
  try { localStorage.removeItem('agnobuilder.lastWorkflowId') } catch { /* ignore */ }
  // Identity store also has module-level state — reset it directly.
  useIdentityStore.setState({ userId: null, email: null, ready: true })
}

test.before(async () => {
  await primeManifestCache()
})

test.beforeEach(() => {
  resetStores()
})

test.after(async () => {
  // fake-indexeddb's open IDB connection keeps the Node event loop
  // alive past the last test. `deleteDatabase` is the only public
  // hook that forces all connections on the named DB to close so
  // the test runner can exit cleanly.
  await new Promise<void>((resolve) => {
    const req = indexedDB.deleteDatabase('agnobuilder-snapshots')
    req.onsuccess = req.onerror = req.onblocked = () => resolve()
  })
  // Belt + suspenders: the workflowStore's module-level
  // autoSaveTimer (and other lingering subscribers) can keep the
  // event loop alive past the last test. Node 18 lacks
  // --test-force-exit, so schedule an unref'd hard exit a tick
  // later — by then the test runner has flushed its summary.
  setTimeout(() => process.exit(0), 50).unref()
})

// ─────────────────────────────────────────────────────────────
// Tests
// ─────────────────────────────────────────────────────────────

test('applySnapshot: projects envelope to canvas state and marks dirty', () => {
  useIdentityStore.setState({ userId: 'alice', email: 'alice@x', ready: true })
  const snap: SnapshotEnvelope = {
    key: snapshotKey('alice', 'wf-1'),
    workflowId: 'wf-1',
    name: 'Recovered',
    description: 'from snapshot',
    nodes: [{
      id: 'n1', type: 'agent', position: { x: 10, y: 20 },
      data: { label: 'X', config: { instructions: 'do thing' } },
    }],
    edges: [],
    savedAt: Date.now(),
    backendUpdatedAt: '2025-01-01T00:00:00Z',
  }
  useWorkflowStore.getState().applySnapshot(snap)
  const s = useWorkflowStore.getState()
  assert.equal(s.workflowId, 'wf-1')
  assert.equal(s.name, 'Recovered')
  assert.equal(s.description, 'from snapshot')
  assert.equal(s.dirty, true)
  assert.equal(s.backendUpdatedAt, '2025-01-01T00:00:00Z')
  assert.equal(s.nodes.length, 1)
  assert.equal(s.nodes[0]!.id, 'n1')
})

test('recordSnapshot: writes current dirty state to IndexedDB', async () => {
  useIdentityStore.setState({ userId: 'alice', email: 'alice@x', ready: true })
  // Seed the canvas with a known state.
  useWorkflowStore.setState({
    workflowId: 'wf-1',
    name: 'Snapshot me',
    description: '',
    nodes: [],
    edges: [],
    dirty: true,
    backendUpdatedAt: '2025-01-01T00:00:00Z',
  })
  await useWorkflowStore.getState().recordSnapshot()
  const got = await getSnapshot(snapshotKey('alice', 'wf-1'))
  assert.ok(got, 'snapshot was written')
  assert.equal(got!.name, 'Snapshot me')
  assert.equal(got!.workflowId, 'wf-1')
  assert.equal(got!.backendUpdatedAt, '2025-01-01T00:00:00Z')
  assert.ok(got!.savedAt > 0)
})

test('recordSnapshot: skips when no userId (pre-identify)', async () => {
  useIdentityStore.setState({ userId: null, email: null, ready: true })
  useWorkflowStore.setState({
    workflowId: 'wf-1', name: 'x', description: '', nodes: [], edges: [],
    dirty: true, backendUpdatedAt: null,
  })
  await useWorkflowStore.getState().recordSnapshot()
  // No userId → no key namespace → nothing written.
  const draft = await getSnapshot(snapshotKey('', 'wf-1'))
  assert.equal(draft, undefined)
})

test('snapshot subscribe: fires recordSnapshot on dirty mutation', async () => {
  useIdentityStore.setState({ userId: 'alice', email: 'alice@x', ready: true })
  // First mutation: seeds state and primes the snapshot subscribe
  // tracking (lastSnapshottedNodes etc).
  const id = useWorkflowStore.getState().addNode('agent', { x: 0, y: 0 })
  // The subscribe is async; give the IDB tx a microtask to commit.
  await new Promise((r) => setTimeout(r, 0))
  const first = await getSnapshot(snapshotKey('alice', null))
  assert.ok(first, 'snapshot written after first mutation')
  assert.equal(first!.nodes.length, 1)

  // Second mutation should also produce a write — but the subscribe
  // dedupes by reference. Mutating via a real action (`updateNodeData`)
  // produces a new nodes[] reference each time, so a fresh put fires.
  useWorkflowStore.getState().updateNodeData(id, { label: 'edited' })
  await new Promise((r) => setTimeout(r, 0))
  const second = await getSnapshot(snapshotKey('alice', null))
  assert.ok(second, 'snapshot rewritten after second mutation')
  // The new envelope has the latest label.
  assert.equal(second!.nodes[0]!.data.label, 'edited')
})

test('after save() succeeds the snapshot is removed', async () => {
  useIdentityStore.setState({ userId: 'alice', email: 'alice@x', ready: true })
  // Seed a saved-style state with workflowId + dirty=true.
  useWorkflowStore.setState({
    workflowId: 'wf-1',
    name: 'Save me',
    description: '',
    nodes: [],
    edges: [],
    dirty: true,
    backendUpdatedAt: '2025-01-01T00:00:00Z',
  })
  // Force a snapshot write so we can verify the delete.
  await useWorkflowStore.getState().recordSnapshot()
  const before = await getSnapshot(snapshotKey('alice', 'wf-1'))
  assert.ok(before, 'snapshot exists pre-save')

  // Stub fetch so `save()` → `workflowsApi.replace` resolves cleanly.
  const originalFetch = globalThis.fetch
  globalThis.fetch = (async () =>
    new Response(
      JSON.stringify({
        id: 'wf-1', name: 'Save me', description: '',
        nodes: [], edges: [], mcpServers: [],
        createdAt: '2025-01-01T00:00:00Z', updatedAt: '2025-01-02T00:00:00Z',
      }),
      { status: 200 },
    )) as typeof fetch
  try {
    await useWorkflowStore.getState().save()
  } finally {
    globalThis.fetch = originalFetch
  }
  // The save() success → dirty=false transition should have
  // triggered the snapshot subscribe's clear-phase delete.
  await new Promise((r) => setTimeout(r, 0))
  const after = await getSnapshot(snapshotKey('alice', 'wf-1'))
  assert.equal(after, undefined, 'snapshot cleared after save')
})

test('cross-user isolation: user B cannot see user A snapshot', async () => {
  useIdentityStore.setState({ userId: 'alice', email: 'a@x', ready: true })
  useWorkflowStore.setState({
    workflowId: 'wf-1', name: 'A draft', description: '', nodes: [], edges: [],
    dirty: true, backendUpdatedAt: null,
  })
  await useWorkflowStore.getState().recordSnapshot()

  // Switch identity to bob — snapshots are keyed by userId, so bob's
  // namespace is empty even though the IndexedDB still has alice's row.
  useIdentityStore.setState({ userId: 'bob', email: 'b@x', ready: true })
  const bobView = await getSnapshot(snapshotKey('bob', 'wf-1'))
  assert.equal(bobView, undefined)
  // Alice's row still there under her key.
  const aliceView = await getSnapshot(snapshotKey('alice', 'wf-1'))
  assert.ok(aliceView)
})