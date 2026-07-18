import { useEffect, useState } from 'react'
import { useWorkflowStore, getPersistedWorkflowId } from './store/workflowStore'
import { useAppUiStore } from './store/appUiStore'
import { useWorkflowListStore } from './store/workflowListStore'
import { useIdentityStore } from './store/identityStore'
import { useChatRunStore } from './store/chatRunStore'
import { useLocale, useT } from './i18n'
import { useThemeApplier } from './lib/useThemeApplier'
import { useFileDrop } from './hooks/useFileDrop'
import { importJsonWorkflow } from './lib/importJsonWorkflow'
import { fetchNodeTypesManifest } from './api/nodeTypes'
import { registerOnApplied } from './store/builderChatActions'
import { rehydratePausedSession } from './store/chatActions'
import {
  snapshotKey,
  getSnapshot,
  deleteSnapshot,
  type SnapshotEnvelope,
} from './lib/snapshotStore'
import { WorkflowToolbar } from './components/Toolbar/WorkflowToolbar'
import { WorkflowCanvas } from './components/Canvas/WorkflowCanvas'
import { PropertyPanel } from './components/PropertyPanel/PropertyPanel'
import { TracePanel } from './components/TracePanel/TracePanel'
import { DropOverlay } from './components/UI/DropOverlay'
import { EmailGateModal } from './components/Identity/EmailGateModal'
import { ChatSidebar } from './components/ChatSidebar/ChatSidebar'
import { SplitPane } from './components/Layout/SplitPane'
import { RestoreToast } from './components/Workflow/RestoreToast'

/** True when focus is on something that should reserve Backspace for
 *  character-deletion (input / textarea / select / contenteditable /
 *  the chat composer etc.). */
function isEditableTarget(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false
  if (el.isContentEditable) return true
  const tag = el.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT'
}

function App() {
  const selectedNodeId = useWorkflowStore((s) => s.selectedNodeId)
  const removeNode = useWorkflowStore((s) => s.removeNode)
  const selectNode = useWorkflowStore((s) => s.selectNode)
  const locale = useLocale()
  const t = useT()

  // — reconcile localStorage with the backend before rendering
  // anything else. While `ready=false` the gate modal stays on top
  // (it watches `userId`, not `ready`); the rest of the UI also
  // stays unmounted so workflow CRUD can't fire with a stale id.
  const identityReady = useIdentityStore((s) => s.ready)
  const initIdentity = useIdentityStore((s) => s.init)
  useEffect(() => {
    void initIdentity()
  }, [initIdentity])

  // Prime the node-types manifest cache on mount so the
  // first call to `useNodeVisuals()` from any consumer returns the
  // manifest instead of the fallback. Without this every consumer
  // would mount twice — once with the fallback and once with the
  // fetched manifest.
  useEffect(() => {
    if (!identityReady) return
    void fetchNodeTypesManifest().catch((err) => {
      console.warn('node-types manifest fetch failed; using fallback', err)
    })
  }, [identityReady])
  // Trace panel state — toggled from the chat header. Lives here (not in
  // the chat store) so opening it doesn't require the chat panel to be
  // visible first.
  const [traceOpen, setTraceOpen] = useState(false)

  // Restore the last-loaded workflow on mount. The workflow store
  // persists the id to localStorage whenever the canvas is in sync
  // with the backend (see workflowStore subscriber); we just need
  // to re-fetch the workflow body here.
  //
  // The persisted id is rebound to the *current* caller on every
  // boot. With switching identity between visits (anonymous →
  // identified email, or "Switch user"), the previous id may belong
  // to a different user. Blindly firing GET /workflows/{id} would
  // 403 for non-members and print a noisy network error in the
  // console. We side-step that by checking the workflow list first
  // (which is the authoritative source of "what does this user own")
  // and only attempting the load if the id is in the list.
  //
  // P1 : after the backend load, peek IndexedDB
  // for a snapshot of unsaved local edits. Two outcomes:
  //   • backend failed (404 / network) + snapshot exists → restore
  //     silently (no choice — backend is the only authoritative copy
  //     and it's gone).
  //   • backend OK + snapshot is newer than backend.updatedAt →
  //     show RestoreToast with Restore / Discard actions.
  // The reconcile helper is extracted from the effect body so the
  // same logic runs whether the workflow is "owned" on cold start or
  // discovered after a list refresh.
  const [restoreOffer, setRestoreOffer] = useState<SnapshotEnvelope | null>(null)
  useEffect(() => {
    if (!identityReady) return
    const id = getPersistedWorkflowId()
    if (!id) return
    if (useWorkflowStore.getState().workflowId === id) return
    let cancelled = false
    const reconcile = async () => {
      if (cancelled) return
      await useWorkflowStore.getState().loadFromBackend(id)
      if (cancelled) return
      const { error, workflowId, backendUpdatedAt } = useWorkflowStore.getState()
      const userId = useIdentityStore.getState().userId
      // P1 — reconcile against IndexedDB.
      const snap = userId ? await getSnapshot(snapshotKey(userId, id)) : undefined
      if (cancelled) return
      if (error || workflowId !== id) {
        // Backend failed. If we have a snapshot, restore silently —
        // the user has unsaved edits and we have no authoritative
        // copy to compare against.
        if (snap) {
          useWorkflowStore.getState().applySnapshot(snap)
        } else {
          // Truly gone. Drop localStorage + clear the empty store.
          useWorkflowStore.setState({
            workflowId: null, error: null, nodes: [], edges: [],
          })
          try { localStorage.removeItem('agnobuilder.lastWorkflowId') } catch { /* ignore */ }
        }
        return
      }
      // Backend OK — does the user have a newer local snapshot?
      if (snap && backendUpdatedAt && snap.savedAt > new Date(backendUpdatedAt).getTime()) {
        setRestoreOffer(snap)
      }
      // / session: if the slim session was paused for
      // human input before the refresh, rehydrate the
      // `pendingConfirmation` chat state so the user sees
      // the pending ask + their in-flight answer input. Fire-
      // and-forget — rehydratePausedSession mutates the chat
      // store synchronously once the GET resolves. We kick it
      // off LAST so a slow /resume doesn't delay the workflow
      // canvas from rendering. (Best-effort: a stale session id
      // 404s silently inside rehydratePausedSession.)
      const currentSessionId = useChatRunStore.getState().sessionId
      if (currentSessionId) {
        void rehydratePausedSession(currentSessionId)
      }
    }
    const listStore = useWorkflowListStore.getState()
    const ownedByCurrentUser = listStore.items.some((w) => w.id === id)
    if (ownedByCurrentUser) {
      void reconcile()
      return () => { cancelled = true }
    }
    // Either the list is empty (cold start) or the id is missing
    // from it. Refresh once, then re-check. If still missing, the
    // stale id gets dropped silently — no GET, no 403, no toast.
    void listStore.refresh().then(() => {
      if (cancelled) return
      const stillOwned = useWorkflowListStore.getState().items.some((w) => w.id === id)
      if (stillOwned) {
        void reconcile()
      } else {
        // workflow id no longer belongs to the current user.
        // Drop it AND its snapshot (if any) so a future switch back
        // doesn't leak the previous user's dirty edits.
        const userId = useIdentityStore.getState().userId
        if (userId) void deleteSnapshot(snapshotKey(userId, id))
        try { localStorage.removeItem('agnobuilder.lastWorkflowId') } catch { /* ignore */ }
      }
    })
    return () => { cancelled = true }
  }, [identityReady])

  // First-visit onboarding: if the user has no persisted workflow AND
  // the backend has no user workflows AND they haven't completed
  // onboarding yet, auto-open the template gallery so they pick a
  // starting point. Skipped if:
  //   - `agnobuilder.lastWorkflowId` exists (they've been here before)
  //   - `agnobuilder.onboarded` is set (they already used the gallery
  //     once — even if they later deleted everything, we don't keep
  //     popping the gallery at them)
  //   - any user workflow exists on the backend (they're not a fresh
  //     user even if localStorage was cleared)
  //
  // The flag is set by `TemplateGalleryModal` whenever the user picks
  // a template OR chooses "Start empty".
  useEffect(() => {
    if (!identityReady) return
    if (getPersistedWorkflowId()) return
    try {
      if (localStorage.getItem('agnobuilder.onboarded')) return
    } catch {
      return  // localStorage unavailable — bail out instead of looping
    }
    const open = useAppUiStore.getState().openTemplates
    const listStore = useWorkflowListStore.getState()
    void listStore.refresh().then(() => {
      // Only auto-open if the backend confirms zero user workflows.
      // Otherwise the user has data we just couldn't see locally.
      if (listStore.items.length === 0) open()
    })
  }, [identityReady])

  // Apply theme → <html class="dark"> + OS scheme listener
  useThemeApplier()
  // Reflect locale → <html lang="…"> (for screen readers, browser UI)
  useEffect(() => {
    document.documentElement.lang = locale
  }, [locale])

  // Global keyboard delete — Delete / Backspace removes the selected node
  // whenever focus is NOT in an editable field. We listen on `window`
  // (not the canvas / panel) so the shortcut works from anywhere on
  // the page — including when the panel or canvas isn't focused.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Delete' && e.key !== 'Backspace') return
      if (isEditableTarget(e.target)) return
      // Skip if a modifier is held (don't hijack Ctrl+Backspace etc.)
      if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return
      const id = useWorkflowStore.getState().selectedNodeId
      if (!id) return
      e.preventDefault()
      removeNode(id)
      selectNode(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [removeNode, selectNode])

  // Global file-drop: drag a .json anywhere on the page → import it.
  // useFileDrop ignores internal palette drags (which use
  // `application/agnobuilder-node-type`) so node-palette → canvas
  // dragging still works.
  const { state: dropState } = useFileDrop({
    accept: (f) =>
      f.name.toLowerCase().endsWith('.json') ||
      f.type === 'application/json',
    onFile: (f) => {
      void importJsonWorkflow(f, t)
    },
  })

  // Builder-chat Apply → lightweight canvas refresh. The action
  // layer calls this listener with the freshly-applied `Workflow`
  // (the backend's apply response is the new source of truth),
  // and we merge it straight into the canvas store — no second
  // GET, no full reload. We register at App mount so the chat
  // sidebar doesn't have to prop-drill the callback through
  // ThreadPrimitive's Message components.
  useEffect(() => {
    return registerOnApplied((wf) => {
      useWorkflowStore.getState().applyTemplateResult(wf)
    })
  }, [])

  return (
    <div className="flex flex-col h-full bg-bg text-ink">
      <WorkflowToolbar />
      <SplitPane
        sidebar={<ChatSidebar />}
        main={
          <main className="flex flex-1 min-h-0">
            {/* The NodePalette chips live at the top of the canvas
                (see `WorkflowCanvas.tsx`); the chat sidebar is the
                left surface for Build/Run chat. PropertyPanel
                slides in from the right when a node is selected. */}
            <div className="flex flex-col flex-1 min-h-0 min-w-0">
              <WorkflowCanvas />
            </div>
            {selectedNodeId && <PropertyPanel nodeId={selectedNodeId} />}
          </main>
        }
      />
      <TracePanel open={traceOpen} onClose={() => setTraceOpen(false)} />
      {dropState === 'hovering' && (
        <DropOverlay
          title={t('toolbar.share.dropTitle')}
          hint={t('toolbar.share.dropHint')}
        />
      )}
      {/* Always-mounted identity gate — it self-hides once `userId`
          is set, and self-shows again after `signOut()`. */}
      <EmailGateModal />
      {/* P1 — RestoreToast. Only mounted when the boot reconcile
          finds an IndexedDB snapshot newer than the backend's
          `updatedAt`. Restore = applySnapshot + delete snapshot;
          Discard = delete snapshot (the backend state stays).
          Auto-dismisses after 6s as Discard (handled inside the
          component). */}
      {restoreOffer && (
        <RestoreToast
          snapshot={restoreOffer}
          onRestore={() => {
            useWorkflowStore.getState().applySnapshot(restoreOffer)
            const userId = useIdentityStore.getState().userId
            if (userId) void deleteSnapshot(snapshotKey(userId, restoreOffer.workflowId))
            setRestoreOffer(null)
          }}
          onDiscard={() => {
            const userId = useIdentityStore.getState().userId
            if (userId) void deleteSnapshot(snapshotKey(userId, restoreOffer.workflowId))
            setRestoreOffer(null)
          }}
        />
      )}
    </div>
  )
}

export default App