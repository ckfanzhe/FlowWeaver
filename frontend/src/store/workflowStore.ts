/**
 * Workflow store — current workflow's nodes/edges, selection, dirty flag, save/load.
 *
 * Persistence is layered:
 *   1. `localStorage` holds the last-loaded workflow id (only when
 *      the canvas is NOT dirty). On boot App.tsx reads this id and
 *      refetches the body from the backend — backend is the source
 *      of truth for committed state.
 *   2. `IndexedDB` (see `snapshotStore.ts`) holds the latest *dirty*
 *      canvas so a refresh / tab-close mid-edit doesn't lose work
 *      (P1, ). App.tsx reconciles on boot: backend
 *      fails → restore snapshot silently; backend OK + snapshot
 *      newer → toast Restore/Discard.
 *   3. `beforeunload` flushes any in-flight dirty edits to IndexedDB
 *      before the tab tears down — bridge for the 800ms auto-save
 *      debounce window.
 */
import { create } from 'zustand'
import type {
  Workflow,
  WorkflowNode,
  WorkflowEdge,
  NodeType,
} from '../types/workflow'
import { workflowsApi } from '../api/workflows'
import { resolveForm } from '../components/PropertyPanel/forms/registry'
import { nodeTypesManifest } from '../api/nodeTypes'
import {
  snapshotKey,
  putSnapshot,
  deleteSnapshot,
  type SnapshotEnvelope,
} from '../lib/snapshotStore'
import { useIdentityStore } from './identityStore'

/**
 * localStorage key for the last-loaded workflow id. Kept tiny — just
 * the id string. We deliberately do NOT persist nodes/edges here:
 * editing happens against the backend's source of truth, and the
 * user can always re-open a workflow from the "Load" menu.
 *
 * Per-user namespacing (`<userId>::<workflowId>` style): the
 * lastWorkflowId is keyed by userId so a different identity on
 * the same browser doesn't inherit the previous user's "last
 * opened" workflow on sign-in. The earlier `agnobuilder.lastWorkflowId`
 * global key was a privacy gap (memory
 * `frontend-snapshot-recovery.md`); this format fixes it.
 */
function lastWorkflowIdKey(userId: string): string {
  return `agnobuilder.lastWorkflowId.${userId}`
}

/**
 * Auto-save debounce window. After the user stops making edits for
 * this long, the canvas commits to the backend. Long enough that a
 * burst of drags during a single drag-drop gesture coalesces into
 * one save; short enough that a refresh shortly after editing sees
 * the latest committed state.
 */
const AUTO_SAVE_DEBOUNCE_MS = 800

function readPersistedWorkflowId(userId: string): string | null {
  try {
    return localStorage.getItem(lastWorkflowIdKey(userId))
  } catch {
    // localStorage can throw in private mode / SSR / sandboxed iframes.
    return null
  }
}

function writePersistedWorkflowId(userId: string, id: string | null): void {
  try {
    if (id) localStorage.setItem(lastWorkflowIdKey(userId), id)
    else localStorage.removeItem(lastWorkflowIdKey(userId))
  } catch {
    /* ignore — persistence is best-effort */
  }
}

interface State {
  workflowId: string | null
  name: string
  description: string
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
  selectedNodeId: string | null
  dirty: boolean
  saving: boolean
  loading: boolean
  error: string | null
  /** Backend's `updatedAt` at the most recent successful `loadFromBackend`.
   *  Captured here so the snapshot reconcile (App.tsx) can compare it
   *  with the local snapshot's `savedAt` and decide whether to offer
   *  a restore. Stays null until the canvas is synced with the
   *  backend at least once — brand-new (un-saved) workflows have no
   *  backend state to be "stale" relative to. */
  backendUpdatedAt: string | null
}

interface Actions {
  // workspace lifecycle
  reset: () => void
  loadFromBackend: (id: string) => Promise<void>
  createNew: (name: string) => Promise<string>
  /** Apply a template's freshly-cloned Workflow to the canvas in one step,
   *  without going through the save round-trip. The backend already
   *  created a new row (POST /from-template) so we're already "saved". */
  applyTemplateResult: (wf: Workflow) => void
  save: () => Promise<void>
  /** P1 — write the current dirty state to IndexedDB. Called by
   *  the snapshot subscribe on every content change while `dirty`
   *  is true, and by `beforeunload` for a last-ditch flush. */
  recordSnapshot: () => Promise<void>
  /** P1 — apply a previously-persisted snapshot to the canvas.
   *  Marks the canvas dirty so the user gets the usual save affordance;
   *  does NOT auto-save (let the user decide whether to keep or
   *  discard). */
  applySnapshot: (snap: SnapshotEnvelope) => void

  // node CRUD
  addNode: (type: NodeType, position: { x: number; y: number }) => string
  updateNodeData: (id: string, patch: Record<string, unknown>) => void
  updateNodePosition: (id: string, position: { x: number; y: number }) => void
  removeNode: (id: string) => void
  duplicateNode: (id: string) => string | null

  // edge CRUD
  addEdge: (edge: WorkflowEdge) => void
  removeEdge: (id: string) => void

  // selection
  selectNode: (id: string | null) => void
}

const initial: State = {
  workflowId: null,
  name: 'Untitled Workflow',
  description: '',
  nodes: [],
  edges: [],
  selectedNodeId: null,
  dirty: false,
  saving: false,
  loading: false,
  error: null,
  backendUpdatedAt: null,
}

let nodeCounter = 0
const nextNodeId = (type: NodeType) => {
  nodeCounter += 1
  return `n-${type}-${nodeCounter}-${Math.random().toString(36).slice(2, 6)}`
}

/**
 * Whether the PropertyPanel can render an editable form for this node
 * type. Drives the canvas's left-click handler: only nodes that
 * resolve to a form (directly, or via the manifest's `extends` chain
 * for presets like wikipedia) get the panel auto-opened.
 *
 * Derived from `FORM_REGISTRY` + the manifest's preset inheritance
 * so that future presets inherit configurability automatically — no
 * need to remember to add them to a hand-maintained set. (Bug
 * history: when wikipedia was added in , a hardcoded
 * CONFIGURABLE_TYPES set silently excluded it, so left-click did
 * nothing while right-click still worked.)
 *
 * The synchronous `nodeTypesManifest()` accessor throws if the
 * fetch hasn't completed yet. That's fine here — by the time a user
 * can click a node, the App-level effect has already populated the
 * cache. If the manifest fetch failed, we fall back to a
 * direct-only check on the FORM_REGISTRY (still correct for all 10
 * base types, just less accommodating for presets).
 */
export const isConfigurable = (type: NodeType): boolean => {
  // Direct hit on the registry covers all 10 base types.
  if (resolveForm(type, null)) return true
  // Preset types inherit form via `extends`. If the manifest hasn't
  // loaded yet, this returns null and we fall through to the direct
  // check above — same behaviour as before the preset existed.
  try {
    return resolveForm(type, nodeTypesManifest()) !== null
  } catch {
    return false
  }
}

export const useWorkflowStore = create<State & Actions>((set, get) => ({
  ...initial,

  reset: () => set({ ...initial }),

  loadFromBackend: async (id) => {
    set({ loading: true, error: null })
    try {
      const wf: Workflow = await workflowsApi.get(id)
      set({
        workflowId: wf.id,
        name: wf.name,
        description: wf.description ?? '',
        nodes: wf.nodes,
        edges: wf.edges,
        selectedNodeId: null,
        dirty: false,
        loading: false,
        backendUpdatedAt: wf.updatedAt,
      })
    } catch (e) {
      set({ error: (e as Error).message, loading: false })
    }
  },

  createNew: async (name) => {
    set({ saving: true, error: null })
    try {
      // Create a TRULY empty workflow — no seeded nodes. The chat is
      // the primary creation surface in this app, so the user must be
      // able to start from a blank canvas and have the LLM build up
      // the graph from their description. Pre-seeding an Agent here
      // would short-circuit that path: every fresh workflow would
      // already contain a node the user didn't ask for, and the chat
      // would then have to explain itself around an unwanted seed.
      //
      // The runtime side keeps its own "non-empty at execution"
      // guard (`runtime_service._require_non_empty`) so an empty
      // workflow can be created and chat-edited freely; trying to
      // RUN it is the only point at which we refuse.
      const wf = await workflowsApi.create({ name, nodes: [], edges: [] })
      set({
        workflowId: wf.id,
        name: wf.name,
        description: wf.description ?? '',
        nodes: [],
        edges: [],
        selectedNodeId: null,
        dirty: false,
        saving: false,
      })
      return wf.id
    } catch (e) {
      set({ error: (e as Error).message, saving: false })
      throw e
    }
  },

  applyTemplateResult: (wf) => {
    set({
      workflowId: wf.id,
      name: wf.name,
      description: wf.description ?? '',
      nodes: wf.nodes,
      edges: wf.edges,
      selectedNodeId: null,
      dirty: false,
      loading: false,
    })
  },

  save: async () => {
    const { workflowId, name, description, nodes, edges } = get()
    set({ saving: true, error: null })
    try {
      let result: Workflow
      if (workflowId) {
        result = await workflowsApi.replace(workflowId, {
          name,
          description,
          nodes,
          edges,
        })
      } else {
        // brand-new workflow: POST the user's current canvas, NOT a seed.
        result = await workflowsApi.create({
          name,
          description,
          nodes,
          edges,
        })
        set({ workflowId: result.id })
      }
      set({ dirty: false, saving: false })
    } catch (e) {
      set({ error: (e as Error).message, saving: false })
    }
  },

  recordSnapshot: async () => {
    // P1 — IndexedDB persistence of dirty state.
    //
    // Compose the envelope from current store state. `key` includes
    // `userId` so user-switch can't leak another user's dirty edits.
    // `workflowId ?? 'draft'` covers the "untitled canvas" case
    // where save() hasn't fired yet to mint a backend row.
    //
    // We deliberately read `useIdentityStore.getState()` inline
    // instead of subscribing — the snapshot path runs once per
    // mutation, and a stale-but-still-correct userId (user just
    // signed out, snapshot for old id stays at old key) is harmless.
    const userId = useIdentityStore.getState().userId
    if (!userId) return  // not identified — no key namespace, skip
    const s = get()
    const env: SnapshotEnvelope = {
      key: snapshotKey(userId, s.workflowId),
      workflowId: s.workflowId,
      name: s.name,
      description: s.description,
      nodes: s.nodes,
      edges: s.edges,
      savedAt: Date.now(),
      backendUpdatedAt: s.backendUpdatedAt,
    }
    await putSnapshot(env)
  },

  applySnapshot: (snap) => {
    // P1 — restore a previously-persisted snapshot. Marks
    // dirty=true so the user gets the save affordance; does NOT
    // auto-save (the explicit user choice is part of the UX).
    set({
      workflowId: snap.workflowId,
      name: snap.name,
      description: snap.description,
      nodes: snap.nodes,
      edges: snap.edges,
      selectedNodeId: null,
      dirty: true,
      backendUpdatedAt: snap.backendUpdatedAt,
      error: null,
    })
  },

  addNode: (type, position) => {
    const id = nextNodeId(type)
    const node: WorkflowNode = {
      id,
      type,
      position,
      data: { label: defaultLabel(type), config: defaultConfig(type) },
    }
    set((s) => ({
      nodes: [...s.nodes, node],
      dirty: true,
      // Drag never opens the property panel — user clicks a node to edit it.
    }))
    return id
  },

  updateNodeData: (id, patch) => {
    set((s) => ({
      nodes: s.nodes.map((n) =>
        n.id === id
          ? { ...n, data: { ...n.data, ...patch } }
          : n,
      ),
      dirty: true,
    }))
  },

  updateNodePosition: (id, position) => {
    set((s) => ({
      nodes: s.nodes.map((n) =>
        n.id === id ? { ...n, position } : n,
      ),
      // Position changes triggered programmatically (e.g. by the
      // import-time spread) should mark the workflow dirty so the user
      // gets a chance to save the new layout. Manual drags already
      // mark dirty through the onNodesChange handler in WorkflowCanvas.
      dirty: true,
    }))
  },

  removeNode: (id) => {
    set((s) => ({
      nodes: s.nodes.filter((n) => n.id !== id),
      edges: s.edges.filter((e) => e.source !== id && e.target !== id),
      selectedNodeId: s.selectedNodeId === id ? null : s.selectedNodeId,
      dirty: true,
    }))
  },

  duplicateNode: (id) => {
    let newId: string | null = null
    set((s) => {
      const orig = s.nodes.find((n) => n.id === id)
      if (!orig) return s
      newId = nextNodeId(orig.type)
      // Deep-clone config so the duplicate doesn't share references with
      // the original (otherwise editing one would mutate the other).
      const clone: WorkflowNode = {
        id: newId,
        type: orig.type,
        // Offset so the duplicate doesn't land on top of the original.
        position: { x: orig.position.x + 40, y: orig.position.y + 40 },
        data: {
          label: orig.data.label,
          config: structuredClone(orig.data.config ?? {}),
        },
      }
      return {
        nodes: [...s.nodes, clone],
        selectedNodeId: newId,
        dirty: true,
      }
    })
    return newId
  },

  addEdge: (edge) => {
    set((s) => {
      // Auto-sync: a branch (mode='if-else') node's outgoing edges
      // drive its `cfg.elseTarget`. The first edge is "then", the
      // second is "else" — mirroring the backend runtime which
      // reads only cfg.elseTarget (or the second edge if absent).
      // Keeping them in sync here means the PropertyPanel always
      // reflects the user's edge wiring.
      // : the legacy `condition` type collapsed
      // to `branch` with a `mode` discriminator; we now check
      // `type==='branch' && config.mode==='if-else'` instead of
      // `type==='condition'`.
      const newEdges = [...s.edges, edge]
      const outgoingFromSource = newEdges.filter((e) => e.source === edge.source)
      const srcNode = s.nodes.find((n) => n.id === edge.source)
      let nodes = s.nodes
      const srcCfg = (srcNode?.data?.config ?? {}) as Record<string, unknown>
      const isIfElseBranch =
        srcNode?.type === 'branch' && srcCfg.mode === 'if-else'
      if (srcNode && isIfElseBranch && outgoingFromSource.length === 2) {
        // The newly added edge is the 2nd → it's the "else" branch.
        const elseTarget = edge.target
        const cfg = (srcNode.data?.config ?? {}) as Record<string, unknown>
        if (cfg.elseTarget !== elseTarget) {
          nodes = s.nodes.map((n) =>
            n.id === srcNode.id
              ? {
                  ...n,
                  data: {
                    ...n.data,
                    config: { ...cfg, elseTarget },
                  },
                }
              : n,
          )
        }
      }
      return { edges: newEdges, nodes, dirty: true }
    })
  },

  removeEdge: (id) => {
    set((s) => {
      const removed = s.edges.find((e) => e.id === id)
      const newEdges = s.edges.filter((e) => e.id !== id)
      if (!removed) {
        return { edges: newEdges, dirty: true }
      }
      // If the removed edge was the "else" branch of a branch (mode=
      // 'if-else') (i.e. it was the 2nd outgoing edge from that
      // branch), clear the node's cfg.elseTarget so the
      // PropertyPanel doesn't keep showing a stale reference.
      const remainingFromSource = newEdges.filter((e) => e.source === removed.source)
      const srcNode = s.nodes.find((n) => n.id === removed.source)
      let nodes = s.nodes
      const srcCfg2 = (srcNode?.data?.config ?? {}) as Record<string, unknown>
      const isIfElseBranchRm =
        srcNode?.type === 'branch' && srcCfg2.mode === 'if-else'
      if (
        srcNode &&
        isIfElseBranchRm &&
        remainingFromSource.length < 2
      ) {
        const cfg = (srcNode.data?.config ?? {}) as Record<string, unknown>
        if (cfg.elseTarget) {
          nodes = s.nodes.map((n) =>
            n.id === srcNode.id
              ? {
                  ...n,
                  data: {
                    ...n.data,
                    config: { ...cfg, elseTarget: '' },
                  },
                }
              : n,
          )
        }
      }
      return { edges: newEdges, nodes, dirty: true }
    })
  },

  selectNode: (id) => set({ selectedNodeId: id }),
}))

function defaultLabel(_type: NodeType): string {
  // Empty by default — components render the localized type name via i18n
  // when the label is empty (see `label || t('nodes.<type>.label')`).
  return ''
}

function defaultConfig(type: NodeType): Record<string, unknown> {
  // F : manifest is the single source of truth for
  // node defaults. The backend's `/api/v1/node-types` endpoint merges
  // `extends` + `overrides.defaultConfig` server-side (see
  // `core.node_types._resolve_entry` / `_merge_preset_on_parent`),
  // so a preset like `wikipedia` arrives with
  // `toolName: "wikipedia_search"`, `baseUrl: "https://en.wikipedia.org"`,
  // etc. — NOT an empty object.
  //
  // Before F ( bug): wikipedia fell through to a hand-
  // written switch in this file and `HttpForm` rendered every field
  // empty. The i18n placeholders (`fetch_user` / `id`) showed
  // in the inputs as if they were the values — deeply confusing for a
  // preset that shouldn't even expose those HTTP examples.
  //
  // structuredClone so the live `entry.defaultConfig` (a shared
  // manifest object) isn't mutated by user edits.
  try {
    const entry = nodeTypesManifest().entries[type]
    if (entry?.defaultConfig) return structuredClone(entry.defaultConfig)
  } catch {
    // Manifest not loaded yet — happens only on the very first paint
    // before App.tsx's effect populates the cache. The palette isn't
    // interactive that early (no entries to drop from), so `{}` is
    // fine. Adding a new node type means adding its defaultConfig to
    // `shared/nodes.manifest.json`, NOT a switch case here.
  }
  return {}
}

// ─────────────────────────────────────────────────────────────────
// Persist the loaded workflow id so a refresh restores the canvas.
//
// Rule: persist `workflowId` only while the workflow is NOT dirty.
// Reasoning:
//   - `dirty === false` means the canvas matches the backend's
//     source of truth (just-loaded or just-saved). It's safe to
//     auto-reload on refresh.
//   - `dirty === true` means the user has uncommitted local edits.
//     Refreshing should NOT silently load the previous saved version
//     (that would clobber their work). The IndexedDB snapshot
//     subscribe (below) takes over for the dirty window — on boot
//     App.tsx checks the snapshot and offers Restore/Discard.
//
// We subscribe (instead of calling writePersistedWorkflowId inside
// each action) so we can't forget to update localStorage when a new
// setter is added. Every state mutation fires this subscriber; the
// body itself only writes when the *meaningful* inputs change
// (`workflowId` + `dirty` + `userId`), so redundant writes are rare:
//   • Same `workflowId` + same `userId` + `dirty=false` re-writes
//     the same value — cheap, idempotent, fine.
//   • `dirty=true` doesn't touch localStorage (the persisted id
//     reflects the last *saved* version, not in-progress edits).
//   • `userId=null` short-circuits (sign-out, anonymous).
//
// Earlier revisions kept a module-level `(lastPersistedId,
// lastPersistedUserId)` cache to skip redundant writes, but the
// cache leaked across tests (module-level `let` is process-global
// in Node's `node --test` runner) and required test-only reset
// hooks. The cache saved microseconds at most; the simpler
// shape here is easier to reason about.
// ─────────────────────────────────────────────────────────────────
useWorkflowStore.subscribe((state) => {
  const id = state.workflowId
  // Per-user namespacing: only write localStorage when we have a
  // current userId (otherwise we'd write under an empty key and
  // mix identities). `useIdentityStore.getState()` reads the live
  // userId so a sign-in between state changes sees the new id.
  const userId = useIdentityStore.getState().userId
  if (!userId) return
  // Only persist when the canvas is in sync with the backend. If the
  // user is mid-edit (dirty), skip — the persisted id (if any) still
  // reflects the last saved version.
  if (id && !state.dirty) {
    writePersistedWorkflowId(userId, id)
  } else if (!id) {
    writePersistedWorkflowId(userId, null)
  }
  // else: id is set AND dirty → leave localStorage untouched.
})

// ─────────────────────────────────────────────────────────────────
// P1 — IndexedDB snapshot subscribe
//
// Mirror of the localStorage subscribe above, but for the dirty
// window. Whenever the canvas is dirty AND its content changed,
// write a snapshot; whenever `dirty` flips back to false (save
// succeeded), delete the now-stale snapshot so the next boot
// doesn't offer to "restore" already-saved work.
//
// We do NOT debounce here — `recordSnapshot` is ~1ms per call
// (single keyPath put on a small object store). Adding debounce
// would mean risking losing the last mutation before the 800ms
// auto-save fires.
// ─────────────────────────────────────────────────────────────────
let lastSnapshottedNodes: WorkflowNode[] | null = null
let lastSnapshottedEdges: WorkflowEdge[] | null = null
let lastSnapshottedName = ''
let lastSnapshottedDesc = ''
useWorkflowStore.subscribe((state, prev) => {
  // Capture phase: dirty AND content actually changed → write.
  if (
    state.dirty &&
    (state.nodes !== lastSnapshottedNodes ||
      state.edges !== lastSnapshottedEdges ||
      state.name !== lastSnapshottedName ||
      state.description !== lastSnapshottedDesc)
  ) {
    lastSnapshottedNodes = state.nodes
    lastSnapshottedEdges = state.edges
    lastSnapshottedName = state.name
    lastSnapshottedDesc = state.description
    void useWorkflowStore.getState().recordSnapshot()
  }
  // Clear phase: dirty flipped false (save succeeded) → drop the
  // snapshot so it doesn't get offered as "restore" next boot.
  // Only delete when workflowId is set — for the `workflowId=null`
  // case (brand-new canvas just saved → server minted a new id),
  // we cleared prev.dirty and the snapshot's key now mismatches
  // because the new id is in state but the snapshot was keyed
  // against `draft`. Worst case: a one-shot orphan entry under
  // `userId::draft`, overwritten by the user's next edit. Cheap.
  if (prev.dirty && !state.dirty && state.workflowId) {
    const userId = useIdentityStore.getState().userId
    if (userId) void deleteSnapshot(snapshotKey(userId, state.workflowId))
  }
})

// ─────────────────────────────────────────────────────────────────
// P1 — beforeunload last-ditch flush
//
// Browser lets `beforeunload` fire async work but won't wait for it.
// We fire-and-forget `recordSnapshot` so any in-flight edits that
// missed the 800ms auto-save debounce window have a fighting chance
// of surviving a tab close. The IndexedDB tx usually commits before
// the tab tears down; on the unlucky edge the last ~1ms is still
// lost. Better than losing the whole 800ms window.
// ─────────────────────────────────────────────────────────────────
if (typeof window !== 'undefined') {
  window.addEventListener('beforeunload', () => {
    const s = useWorkflowStore.getState()
    if (s.dirty) {
      void useWorkflowStore.getState().recordSnapshot()
    }
  })
}

// ─────────────────────────────────────────────────────────────────
// Auto-save
//
// Whenever `dirty` flips to `true`, schedule a `save()` call after
// `AUTO_SAVE_DEBOUNCE_MS` of inactivity. Each subsequent edit resets
// the timer — a burst of drags during one gesture coalesces into
// exactly one round-trip.
//
// We deliberately do NOT save when `dirty` is false (nothing to
// commit) or when `name` is empty (a brand-new workflow with no
// name would otherwise hit the backend's name-required validation
// during typing).
//
// The in-flight guard prevents two saves from racing — if the user
// types fast enough that one save hasn't completed by the time the
// next debounce fires, we re-arm the timer to fire again right after
// the current save resolves.
// ─────────────────────────────────────────────────────────────────
let autoSaveTimer: ReturnType<typeof setTimeout> | null = null
let autoSaveInFlight = false
let autoSavePending = false

function _scheduleAutoSave(): void {
  if (autoSaveTimer !== null) clearTimeout(autoSaveTimer)
  autoSaveTimer = setTimeout(() => {
    autoSaveTimer = null
    void _runAutoSave()
  }, AUTO_SAVE_DEBOUNCE_MS)
}

async function _runAutoSave(): Promise<void> {
  if (autoSaveInFlight) {
    // Another save is already running; remember to re-check after
    // it completes. We don't reset `dirty` manually — the save()
    // action does that on success.
    autoSavePending = true
    return
  }
  const { name, dirty } = useWorkflowStore.getState()
  if (!dirty || !name.trim()) return
  autoSaveInFlight = true
  try {
    await useWorkflowStore.getState().save()
  } finally {
    autoSaveInFlight = false
    // If `dirty` flipped back on during the round-trip (rare — only
    // when an external write happens mid-save), fire another save.
    if (autoSavePending || useWorkflowStore.getState().dirty) {
      autoSavePending = false
      _scheduleAutoSave()
    }
  }
}

useWorkflowStore.subscribe((state, prev) => {
  // Only rearm when `dirty` actually transitions to true; re-arming
  // on every keystroke (even when dirty was already true) is fine
  // for the debounce but noisy — the condition above keeps the
  // subscription cheap.
  if (!state.dirty) return
  if (prev.dirty === state.dirty) return
  // Skip if the user just cleared the name (empty-name guard).
  if (!state.name.trim()) return
  _scheduleAutoSave()
})

/**
 * Read the persisted workflow id (if any) for `userId` at module load.
 * Callers use this to decide whether to call `loadFromBackend` on
 * app mount. Per-user namespacing: returns null when `userId` is
 * falsy so an unauthenticated browser read never accidentally
 * picks up another user's last-opened workflow.
 */
export function getPersistedWorkflowId(userId: string): string | null {
  if (!userId) return null
  return readPersistedWorkflowId(userId)
}