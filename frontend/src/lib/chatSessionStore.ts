/**
 * Chat-session persistence — IndexedDB-backed envelope store for
 * the two chat-sidebar transcripts (`useChatRunStore`,
 * `useBuilderChatStore`). Survives page refresh / accidental tab
 * close; cleared on signOut.
 *
 * Mirrors `snapshotStore.ts` (canvas dirty-state persistence) in
 * three ways:
 *
 *   1. Per-call open + close (no connection caching). `idb.openDB`
 *      resolves a fresh connection each time; we `close()` in the
 *      `finally`. The cost is dominated by JSON serialization, not
 *      the open. See memory `frontend-snapshot-recovery.md` §1 for
 *      the rationale (caching leaks the fake-indexeddb handle past
 *      the Node test runner's lifetime).
 *
 *   2. Composite key `userId::workflowId` (or `userId::draft` when
 *      the workflow is unnamed). Prevents user A's chat from being
 *      rehydrated into user B's session after `signOut` /
 *      `switchUser`. Same convention as `snapshotStore`.
 *
 *   3. Silent no-op on failure. private-mode / quota errors don't
 *      crash the chat; the user loses persistence but the chat
 *      keeps working.
 *
 * Run:  npx tsx --test src/lib/__tests__/chatSessionStore.test.ts
 */
import { openDB, type IDBPDatabase } from 'idb'
import type { ChatMessage, PendingConfirmation } from '../store/sseClient'
import type { BuilderChatMessage, BuilderDiff } from '../types/chatBuilder'

const DB_NAME = 'agnobuilder-chat-sessions'
const DB_VERSION = 1
export const STORE_RUN = 'chat-run'
export const STORE_BUILD = 'chat-build'

/**
 * Composite key: `userId::workflowId` (or `userId::draft` when the
 * workflow is unnamed). Kept as a named helper so the two stores
 * format it identically and don't accidentally orphan each other's
 * writes.
 */
export function chatSessionKey(
  userId: string,
  workflowId: string | null,
): string {
  return `${userId}::${workflowId ?? 'draft'}`
}

function _openDb(): Promise<IDBPDatabase<unknown>> {
  return openDB(DB_NAME, DB_VERSION, {
    upgrade(db) {
      if (!db.objectStoreNames.contains(STORE_RUN)) {
        db.createObjectStore(STORE_RUN, { keyPath: 'key' })
      }
      if (!db.objectStoreNames.contains(STORE_BUILD)) {
        db.createObjectStore(STORE_BUILD, { keyPath: 'key' })
      }
    },
  })
}

async function _db(): Promise<IDBPDatabase<unknown> | null> {
  if (typeof indexedDB === 'undefined') return null
  try {
    return await _openDb()
  } catch (err) {
    console.warn('chatSessionStore: openDB failed; persistence disabled', err)
    return null
  }
}

/**
 * Run-mode envelope. `sessionId` + `pendingConfirmation` are
 * included even though `busy` / `error` are not — `sessionId` lets
 * `rehydratePausedSession` reconnect to a still-paused HITL session
 * after refresh, and `pendingConfirmation` is what the user sees
 * in the chat panel mid-prompt. `busy` would always be `false`
 * after a refresh (stream is dead) so we don't carry it.
 */
export interface RunChatEnvelope {
  key: string
  messages: ChatMessage[]
  sessionId: string | null
  pendingConfirmation: PendingConfirmation | null
  /** Date.now() at write — single timestamp, no timezone ambiguity. */
  savedAt: number
}

/**
 * Build-mode envelope. Carries the staged `diff` so a refresh mid-
 * `Apply` review doesn't lose the user's half-decided changes, and
 * `selectedPresetId` so the model picker remembers the choice.
 * `busy` / `error` excluded for the same reason as Run.
 */
export interface BuildChatEnvelope {
  key: string
  messages: BuilderChatMessage[]
  diff: BuilderDiff | null
  finished: boolean
  selectedPresetId: string | null
  /** Date.now() at write — single timestamp, no timezone ambiguity. */
  savedAt: number
}

export type ChatEnvelope = RunChatEnvelope | BuildChatEnvelope

async function _putEnvelope(
  store: string,
  env: ChatEnvelope,
): Promise<void> {
  const db = await _db()
  if (!db) return
  try {
    await db.put(store, env)
  } catch (err) {
    console.warn(`chatSessionStore: ${store} put failed`, err)
  } finally {
    db.close()
  }
}

async function _getEnvelope<T extends ChatEnvelope>(
  store: string,
  key: string,
): Promise<T | undefined> {
  const db = await _db()
  if (!db) return undefined
  try {
    return (await db.get(store, key)) as T | undefined
  } catch (err) {
    console.warn(`chatSessionStore: ${store} get failed`, err)
    return undefined
  } finally {
    db.close()
  }
}

async function _deleteEnvelope(
  store: string,
  key: string,
): Promise<void> {
  const db = await _db()
  if (!db) return
  try {
    await db.delete(store, key)
  } catch (err) {
    console.warn(`chatSessionStore: ${store} delete failed`, err)
  } finally {
    db.close()
  }
}

/**
 * Iterate every entry in `store` whose key starts with
 * `${userId}::` and delete it. Used by `signOut` to wipe the
 * departing user's chat so the next identity on the same browser
 * doesn't inherit anything.
 */
async function _deleteUserPrefix(store: string, userId: string): Promise<void> {
  const db = await _db()
  if (!db) return
  try {
    const tx = db.transaction(store, 'readwrite')
    let cursor = await tx.objectStore(store).openCursor()
    const prefix = `${userId}::`
    while (cursor) {
      const key = cursor.key as string
      if (key.startsWith(prefix)) {
        await cursor.delete()
      }
      cursor = await cursor.continue()
    }
    await tx.done
  } catch (err) {
    console.warn(`chatSessionStore: ${store} deleteUserPrefix failed`, err)
  } finally {
    db.close()
  }
}

// ─────────────────────────────────────────────────────────────────
// Public API
// ─────────────────────────────────────────────────────────────────
export async function putRunChat(env: RunChatEnvelope): Promise<void> {
  await _putEnvelope(STORE_RUN, env)
}
export async function putBuildChat(env: BuildChatEnvelope): Promise<void> {
  await _putEnvelope(STORE_BUILD, env)
}
export async function getRunChat(
  key: string,
): Promise<RunChatEnvelope | undefined> {
  return _getEnvelope<RunChatEnvelope>(STORE_RUN, key)
}
export async function getBuildChat(
  key: string,
): Promise<BuildChatEnvelope | undefined> {
  return _getEnvelope<BuildChatEnvelope>(STORE_BUILD, key)
}
export async function deleteRunChat(key: string): Promise<void> {
  await _deleteEnvelope(STORE_RUN, key)
}
export async function deleteBuildChat(key: string): Promise<void> {
  await _deleteEnvelope(STORE_BUILD, key)
}

/**
 * Wipe every entry in both stores whose key starts with
 * `${userId}::`. Called from `identityStore.signOut` so a
 * subsequent sign-in on the same browser can't read the previous
 * user's chat. Fire-and-forget — the caller doesn't need to wait
 * before proceeding with the in-memory reset.
 */
export async function deleteUserChats(userId: string): Promise<void> {
  if (!userId) return
  await Promise.all([
    _deleteUserPrefix(STORE_RUN, userId),
    _deleteUserPrefix(STORE_BUILD, userId),
  ])
}