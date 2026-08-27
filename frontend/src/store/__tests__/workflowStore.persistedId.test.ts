/**
 * Tests for `workflowStore`'s per-user `lastWorkflowId`
 * localStorage persistence.
 *
 * Regression:  before this change the persisted id used a single
 * global localStorage key, so identity A's "last opened workflow"
 * leaked into identity B's sign-in on the same browser — and
 * identity A signing back in found their key wiped by the
 * previous signOut's subscriber. The fix namespaces the key by
 * `userId` and skips the write when no user is signed in, so:
 *
 *   1. A and B each have their own `lastWorkflowId` slot.
 *   2. signOut doesn't wipe A's slot just because workflowStore
 *      is reset to `workflowId: null` — the subscriber short-
 *      circuits when there's no current userId.
 *   3. A signs back in: A's workflowId is still in localStorage
 *      and gets reloaded by App.tsx's reconcile effect.
 *
 * Run:  npx tsx --test src/store/__tests__/workflowStore.persistedId.test.ts
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import 'fake-indexeddb/auto'

// Minimal `localStorage` polyfill for Node 18 — the production
// code wraps every read/write in try/catch and silently degrades,
// but the tests below ASSERT against the storage layer, so we
// need a real `Storage` object on globalThis. A Map is enough:
// localStorage only exposes `getItem` / `setItem` / `removeItem` /
// `length` / `key` to our code, and our tests use exactly that
// surface.
class _MemoryStorage implements Storage {
  private store = new Map<string, string>()
  get length(): number { return this.store.size }
  key(i: number): string | null {
    return [...this.store.keys()][i] ?? null
  }
  getItem(k: string): string | null { return this.store.get(k) ?? null }
  setItem(k: string, v: string): void { this.store.set(k, String(v)) }
  removeItem(k: string): void { this.store.delete(k) }
  clear(): void { this.store.clear() }
}
;(globalThis as { localStorage?: Storage }).localStorage ??= new _MemoryStorage()

import { useIdentityStore } from '../identityStore'
import { useWorkflowStore } from '../workflowStore'

function resetAll(): void {
  // Drop every per-user lastWorkflowId entry the test may have
  // written, so test runs don't cross-contaminate.
  try {
    const keys: string[] = []
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i)
      if (k && k.startsWith('agnobuilder.lastWorkflowId.')) keys.push(k)
    }
    for (const k of keys) localStorage.removeItem(k)
  } catch {
    /* ignore */
  }
  useIdentityStore.setState({
    userId: null,
    email: null,
    ready: true,
    error: null,
  })
  useWorkflowStore.getState().reset()
}

test.beforeEach(() => {
  resetAll()
})

// ─────────────────────────────────────────────────────────────────
// Keying
// ─────────────────────────────────────────────────────────────────
test('writes to the userId-namespaced key (not the global one)', () => {
  useIdentityStore.setState({ userId: 'alice', ready: true })
  useWorkflowStore.setState({ workflowId: 'wf-A', dirty: false })
  // Force the subscriber to fire by triggering another state change.
  // (zustand subscribers fire on every set; the global guard inside
  // the subscriber prevents a redundant re-write, but the FIRST set
  // already triggered the write.)
  assert.equal(
    localStorage.getItem('agnobuilder.lastWorkflowId.alice'),
    'wf-A',
  )
  // The legacy global key must NOT exist.
  assert.equal(localStorage.getItem('agnobuilder.lastWorkflowId'), null)
})

test('two users with two workflows write to two separate slots', () => {
  useIdentityStore.setState({ userId: 'alice', ready: true })
  useWorkflowStore.setState({ workflowId: 'wf-A', dirty: false })
  // Switch user — A's slot must stay intact, B's slot must be
  // written with B's workflowId.
  useIdentityStore.setState({ userId: 'bob', ready: true })
  useWorkflowStore.setState({ workflowId: 'wf-B', dirty: false })
  assert.equal(
    localStorage.getItem('agnobuilder.lastWorkflowId.alice'),
    'wf-A',
  )
  assert.equal(
    localStorage.getItem('agnobuilder.lastWorkflowId.bob'),
    'wf-B',
  )
})

test('signOut (userId=null) does not clear the per-user slot', () => {
  useIdentityStore.setState({ userId: 'alice', ready: true })
  useWorkflowStore.setState({ workflowId: 'wf-A', dirty: false })
  // Sign-out sequence: identityStore sets userId=null, then
  // workflowStore.reset() flips workflowId=null. The subscriber
  // short-circuits on the missing userId, so neither alice's slot
  // nor any other user's slot is touched.
  useIdentityStore.setState({ userId: null, ready: true })
  useWorkflowStore.getState().reset()
  assert.equal(
    localStorage.getItem('agnobuilder.lastWorkflowId.alice'),
    'wf-A',
  )
})

test('sign-in after sign-out: original user\'s slot is still readable', () => {
  // The full regression scenario the user reported:
  //   1. A signs in, opens wf-A → alice slot = wf-A
  //   2. A signs out (workflowStore reset, subscriber skips)
  //   3. B signs in, opens wf-B → bob slot = wf-B
  //   4. B signs out
  //   5. A signs in again → alice slot STILL = wf-A
  useIdentityStore.setState({ userId: 'alice', ready: true })
  useWorkflowStore.setState({ workflowId: 'wf-A', dirty: false })

  useIdentityStore.setState({ userId: null, ready: true })
  useWorkflowStore.getState().reset()

  useIdentityStore.setState({ userId: 'bob', ready: true })
  useWorkflowStore.setState({ workflowId: 'wf-B', dirty: false })

  useIdentityStore.setState({ userId: null, ready: true })
  useWorkflowStore.getState().reset()

  // A signs back in — their slot survives the round-trip.
  useIdentityStore.setState({ userId: 'alice', ready: true })
  assert.equal(
    localStorage.getItem('agnobuilder.lastWorkflowId.alice'),
    'wf-A',
  )
  // B's slot is also intact.
  assert.equal(
    localStorage.getItem('agnobuilder.lastWorkflowId.bob'),
    'wf-B',
  )
})

// ─────────────────────────────────────────────────────────────────
// Subscriber guards
// ─────────────────────────────────────────────────────────────────
test('dirty=true does NOT persist the workflowId (mid-edit guard)', () => {
  useIdentityStore.setState({ userId: 'alice', ready: true })
  // dirty=true + workflowId set → subscriber skips (the user is
  // mid-edit; the persisted id should reflect the last saved
  // version, not the in-progress draft).
  useWorkflowStore.setState({ workflowId: 'wf-A', dirty: true })
  assert.equal(
    localStorage.getItem('agnobuilder.lastWorkflowId.alice'),
    null,
  )
  // After save (dirty=false), the next state change persists.
  useWorkflowStore.setState({ workflowId: 'wf-A', dirty: false })
  assert.equal(
    localStorage.getItem('agnobuilder.lastWorkflowId.alice'),
    'wf-A',
  )
})

test('switching from dirty=true to a new workflowId persists the new id when dirty is cleared', () => {
  // Edge case: user is mid-edit on wf-A, switches to wf-B (also
  // dirty at first), then saves wf-B. The persisted id should
  // become wf-B — not stay stuck on whatever wf-A's last saved
  // id was.
  useIdentityStore.setState({ userId: 'alice', ready: true })
  useWorkflowStore.setState({ workflowId: 'wf-A', dirty: false })
  useWorkflowStore.setState({ workflowId: 'wf-A', dirty: true })
  // Switch workflow while still dirty — subscriber still skips
  // because dirty=true.
  useWorkflowStore.setState({ workflowId: 'wf-B', dirty: true })
  assert.equal(
    localStorage.getItem('agnobuilder.lastWorkflowId.alice'),
    'wf-A',
  )
  // Save the new workflow — dirty clears, persisted id updates.
  useWorkflowStore.setState({ workflowId: 'wf-B', dirty: false })
  assert.equal(
    localStorage.getItem('agnobuilder.lastWorkflowId.alice'),
    'wf-B',
  )
})

test('subscriber re-writes the same id on unrelated state changes (no-op)', () => {
  // After dropping the module-level cache, the subscriber fires on
  // every store mutation but only acts when workflowId/dirty change
  // meaningfully. An unrelated mutation (renaming the workflow)
  // re-writes the same `workflowId` value — idempotent and free.
  // The behavioural guarantee we still care about: the slot's value
  // matches the loaded workflowId after the dust settles.
  useIdentityStore.setState({ userId: 'alice', ready: true })
  useWorkflowStore.setState({ workflowId: 'wf-A', dirty: false })
  const afterFirst = localStorage.getItem('agnobuilder.lastWorkflowId.alice')
  // Trigger an unrelated state change.
  useWorkflowStore.setState({ name: 'Renamed' })
  const afterSecond = localStorage.getItem('agnobuilder.lastWorkflowId.alice')
  assert.equal(afterFirst, afterSecond)
  assert.equal(afterSecond, 'wf-A')
})

// chatSessionStore loaded transitively by identityStore (via
// builderChatStore) opens an IDB connection + has a 300ms debounced
// auto-save timer. Both keep the Node event loop alive past the
// last test; mirror the cleanup pattern from
// `workflowStore.snapshot.test.ts`.
test.after(async () => {
  await new Promise<void>((resolve) => {
    const req = indexedDB.deleteDatabase('agnobuilder-chat-sessions')
    req.onsuccess = req.onerror = req.onblocked = () => resolve()
  })
  await new Promise<void>((resolve) => {
    const req = indexedDB.deleteDatabase('agnobuilder-snapshots')
    req.onsuccess = req.onerror = req.onblocked = () => resolve()
  })
  setTimeout(() => process.exit(0), 50).unref()
})