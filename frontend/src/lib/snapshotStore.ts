/**
 * IndexedDB snapshot store — survives hard refresh / accidental tab
 * close while the canvas is dirty. Per SPEC P1, the canvas's
 * source of truth is still the backend, but during a `dirty` window
 * (between user edit and the 800ms auto-save) a refresh would
 * otherwise silently drop the edits. This module persists the
 * latest dirty state and lets App.tsx reconcile on the next boot.
 *
 * Keying: `userId::workflowId` for saved workflows, `userId::draft`
 * for unnamed canvases (workflowId === null). The userId prefix is
 * what stops user A's snapshot from being offered to user B after a
 * `signOut` / `switchUser` (the identity gate already guards the
 * loadFromBackend side, but the snapshot side needs the same
 * isolation so a stale browser tab can't leak dirty edits across
 * identities).
 *
 * SSR / private-mode / sandboxed iframes: indexedDB may be
 * unavailable. All write/read helpers degrade to no-ops rather than
 * throw, so a failed persistence never breaks the canvas.
 *
 * Run:  npx tsx --test src/lib/__tests__/snapshotStore.test.ts
 */
import { openDB } from 'idb'
import type { WorkflowNode, WorkflowEdge } from '../types/workflow'

const SNAPSHOT_DB_NAME = 'agnobuilder-snapshots'
const SNAPSHOT_DB_VERSION = 1
const SNAPSHOT_STORE = 'snapshots'

/**
 * On-disk envelope. Includes the original `key` so the object store
 * can use `keyPath: 'key'` (one fewer index to maintain).
 * `backendUpdatedAt` is the WorkflowRead.updatedAt captured at the
 * last `loadFromBackend` success — used by App.tsx to decide whether
 * the local snapshot is newer than the server's view.
 */
export interface SnapshotEnvelope {
  key: string
  workflowId: string | null
  name: string
  description: string
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
  /** Date.now() at write — single timestamp, no timezone ambiguity. */
  savedAt: number
  /** ISO string from WorkflowRead.updatedAt at last load, or null
   *  if the canvas was never synced with the backend. */
  backendUpdatedAt: string | null
}

/**
 * Composite key: `userId::workflowId` (or `userId::draft`). Kept as
 * a named helper so callers don't accidentally format it differently
 * and orphan their own writes.
 */
export function snapshotKey(userId: string, workflowId: string | null): string {
  return `${userId}::${workflowId ?? 'draft'}`
}

function _openDb(): Promise<IDBPDatabase> {
  return openDB(SNAPSHOT_DB_NAME, SNAPSHOT_DB_VERSION, {
    upgrade(db) {
      if (!db.objectStoreNames.contains(SNAPSHOT_STORE)) {
        db.createObjectStore(SNAPSHOT_STORE, { keyPath: 'key' })
      }
    },
  })
}

async function _db(): Promise<IDBPDatabase | null> {
  if (typeof indexedDB === 'undefined') return null
  try {
    // Open (and close) a fresh connection per call. `idb`'s openDB
    // resolves a brand-new IDBDatabase each time; we `close()` it
    // after the transaction completes. This keeps the per-call cost
    // in the sub-millisecond range while avoiding module-level
    // handle caching — which kept Node's test runner alive past the
    // last test (and would complicate browser unload cleanup).
    const db = await _openDb()
    return db
  } catch (err) {
    console.warn('snapshotStore: openDB failed; snapshots disabled', err)
    return null
  }
}

/**
 * Upsert a snapshot. Silent on failure (private mode, quota, etc.) —
 * the canvas shouldn't break because persistence is down. Closes
 * the per-call DB connection on the way out so the handle doesn't
 * outlive the transaction.
 */
export async function putSnapshot(env: SnapshotEnvelope): Promise<void> {
  const db = await _db()
  if (!db) return
  try {
    await db.put(SNAPSHOT_STORE, env)
  } catch (err) {
    console.warn('snapshotStore: put failed', err)
  } finally {
    db.close()
  }
}

/**
 * Read a snapshot by composite key. Returns undefined when no entry
 * exists OR when IndexedDB is unavailable — App.tsx treats both as
 * "no snapshot".
 */
export async function getSnapshot(
  key: string,
): Promise<SnapshotEnvelope | undefined> {
  const db = await _db()
  if (!db) return undefined
  try {
    return await db.get(SNAPSHOT_STORE, key)
  } catch (err) {
    console.warn('snapshotStore: get failed', err)
    return undefined
  } finally {
    db.close()
  }
}

/**
 * Delete a snapshot by composite key. Called after a successful save
 * (the snapshot is no longer "unsaved local edits") or when the user
 * explicitly discards a recovered snapshot.
 */
export async function deleteSnapshot(key: string): Promise<void> {
  const db = await _db()
  if (!db) return
  try {
    await db.delete(SNAPSHOT_STORE, key)
  } catch (err) {
    console.warn('snapshotStore: delete failed', err)
  } finally {
    db.close()
  }
}