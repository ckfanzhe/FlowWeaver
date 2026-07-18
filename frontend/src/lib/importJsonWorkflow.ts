/**
 * Shared workflow JSON import flow — used by:
 *   - the More menu's "Import JSON…" file picker
 *   - the global drag-and-drop overlay
 *
 * Reads a File, parses it, sends it to the backend, and loads the
 * resulting workflow onto the canvas. Errors are reported via the
 * workflow store's `error` field so they show up in the toolbar.
 *
 * After loading, runs `spreadNodes` over the imported positions so any
 * nodes that came in stacked (e.g. all at the canvas origin) get
 * pushed apart into a readable layout. The workflow is marked dirty
 * iff at least one node actually moved.
 */
import { workflowsApi } from '../api/workflows'
import { useWorkflowStore } from '../store/workflowStore'
import { useChatStore } from '../store/chatStore'
import { spreadNodes } from './layout'

/** i18n function with the same shape as `useT()`. We accept it as a
 *  parameter so the helper doesn't need to subscribe to locale changes. */
type T = (key: string, vars?: Record<string, string | number>) => string

export interface ImportResult {
  /** Set to true on success. */
  ok: boolean
  /** Set on failure (one of: bad-json, rejected-by-backend, network). */
  error?: string
  /** The new workflow's name, on success. */
  name?: string
}

export async function importJsonWorkflow(file: File, t: T): Promise<ImportResult> {
  // 1. Cap file size to avoid runaway memory
  const MAX = 5 * 1024 * 1024 // 5 MiB
  if (file.size > MAX) {
    const msg = t('toolbar.share.importTooBigBody', {
      size: `${(file.size / 1024 / 1024).toFixed(1)} MB`,
      max: '5 MB',
    })
    useWorkflowStore.setState({ error: msg })
    return { ok: false, error: msg }
  }

  // 2. Read + parse JSON
  const text = await file.text()
  let envelope: unknown
  try {
    envelope = JSON.parse(text)
  } catch (err) {
    const msg = t('toolbar.share.importUnparsedBody', { error: (err as Error).message })
    useWorkflowStore.setState({ error: msg })
    return { ok: false, error: msg }
  }

  // 3. Quick client-side check: must look like an agno envelope. The
  //    backend will re-validate everything; this is just a friendly
  //    early exit.
  if (
    typeof envelope !== 'object' ||
    envelope === null ||
    (envelope as Record<string, unknown>).kind !== 'agnobuilder.workflow'
  ) {
    const msg = t('toolbar.share.importWrongKindBody', { file: file.name })
    useWorkflowStore.setState({ error: msg })
    return { ok: false, error: msg }
  }

  // 4. Send to backend
  try {
    const created = await workflowsApi.importJson(envelope)
    // 5. Load the new workflow onto the canvas
    await useWorkflowStore.getState().loadFromBackend(created.id)
    // 6. Spread any overlapping nodes. We do this AFTER load so the
    //    canvas shows the spread layout in a single paint (no flash of
    //    stacked-then-arranged). Skip if the JSON already had clean
    //    positions — `moved` will be false for every node.
    const { nodes, updateNodePosition } = useWorkflowStore.getState()
    const results = spreadNodes(nodes)
    let anyMoved = false
    for (const r of results) {
      if (!r.moved) continue
      anyMoved = true
      updateNodePosition(r.id, r.position)
    }
    // If nothing moved, restore the clean (non-dirty) state the load
    // set up — otherwise the user would see "unsaved" just because
    // they imported a perfectly-spaced workflow.
    if (!anyMoved) {
      useWorkflowStore.setState({ dirty: false })
    }
    useChatStore.getState().hidePanel()
    return { ok: true, name: created.name }
  } catch (err) {
    const msg = t('toolbar.share.importRejectedBody', { error: (err as Error).message })
    useWorkflowStore.setState({ error: msg })
    return { ok: false, error: msg }
  }
}
