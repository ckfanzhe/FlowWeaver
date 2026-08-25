/**
 * Shared base layout for custom nodes: header with icon+label, body for
 * type-specific content, status footer, input handle on the left,
 * output handle on the right.
 *
 * Trace-driven feedback (P3 trace-overhaul, ):
 *   - The currently-running (or errored) node gets the **same accent
 *     ring** as a selected node. We deliberately reuse the selection
 *     style (`ring-[3px] ring-accent shadow-lg scale-[1.02]`) instead
 *     of inventing a new green/red halo — the canvas already had a
 *     clear "selected" affordance, and the trace just borrows it to
 *     answer "where is the workflow right now?" at a glance.
 *   - A status footer at the bottom of every node shows the latest
 *     run's outcome: "Running…", "✓ 230ms · 142 tok", or
 *     "✗ <error>". Status is hidden until the node has actually been
 *     touched by a run, so freshly-loaded canvases stay clean.
 *   - Successful runs leave no ring (only the footer row), keeping the
 *     canvas visually quiet. Errored nodes keep the accent ring until
 *     the next run starts (when `useTraceStore.reset()` clears
 *     statuses) — the footer shows the error reason in detail.
 *
 * While the user is dragging a connection from another node, the canvas
 * sets `data.unreachable = true` whenever THIS node can't be wired to
 * the drag source. We render that with reduced opacity and a tooltip
 * explaining why (so the user sees a clear "can't connect" affordance
 * instead of a red line).
 */
import { Handle, Position, useNodeId, useStore } from '@xyflow/react'
import { memo } from 'react'
import type { NodeType } from '../../types/workflow'
import { useT } from '../../i18n'
import { useTraceStore, type NodeStatus, type TraceEntry } from '../../store/traceStore'
import { useNodeVisuals, resolveVisual, type NodeVisual } from './nodeStyles'

// Generic visual used when a node type isn't in the manifest AND
// has no parent registered (typically: a stale workflow referencing
// a node type that has since been removed). Matches the slate
// styling reserved for tool-source types so the node still renders
// legibly — without this fallback the canvas would crash on
// `v.color` / `v.Icon` whenever it encounters a removed type.
const GENERIC_VISUAL: NodeVisual = {
  color: 'border-slate-400/50 bg-slate-100 dark:bg-slate-800',
  text: 'text-slate-700 dark:text-slate-300',
  // No icon available — BaseNode renders nothing in the icon slot
  // when Icon is undefined; the label still surfaces the node id.
  Icon: () => null,
  i18nKey: 'unknown' as NodeType,
  displayName: 'Unknown node',
  paletteOrder: 999,
  category: 'executable',
}

interface Props {
  type: NodeType
  label: string
  selected?: boolean
  hasInput?: boolean
  hasOutput?: boolean
  children?: React.ReactNode
  /** React Flow's `id` for the node — used to look up its trace status. */
  nodeId?: string
}

/**
 * Read the current node's `data` field from React Flow's internal
 * store. Cheaper than threading props through every wrapper.
 */
function useNodeData(id: string | undefined): Record<string, unknown> | undefined {
  return useStore((s) => (id ? s.nodeLookup.get(id)?.data : undefined))
}

/**
 * Footer row showing the latest run's outcome for this node. Hidden
 * for nodes the trace hasn't touched yet so the canvas doesn't fill with
 * empty footers before the first run.
 */
function StatusFooter({
  entry,
  status,
  t,
}: {
  entry: TraceEntry | null
  status: NodeStatus | undefined
  // Matches `i18n.t` / `useT()` so we can pass the hook's `t` directly.
  t: (key: string, vars?: Record<string, string | number>) => string
}) {
  if (!entry) return null
  if (status === 'running') {
    return (
      <div
        data-testid="node-status-running"
        className="border-t border-black/10 px-3 py-1 text-[10px] font-mono text-emerald-700 dark:text-emerald-300 flex items-center gap-1.5"
      >
        <span className="inline-block w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
        <span>{t('node.status.running')}</span>
      </div>
    )
  }
  if (status === 'ok') {
    const dur = entry.durationMs
    return (
      <div
        data-testid="node-status-ok"
        className="border-t border-black/10 px-3 py-1 text-[10px] font-mono text-ink-muted flex items-center gap-1.5 flex-wrap"
      >
        <span className="text-emerald-700 dark:text-emerald-300">✓</span>
        {dur !== null && <span>{t('node.status.completedMs', { ms: dur })}</span>}
        {entry.tokens && (
          <span className="opacity-70">
            · {t('node.status.tokens', { count: entry.tokens.total })}
          </span>
        )}
      </div>
    )
  }
  if (status === 'error') {
    return (
      <div
        data-testid="node-status-error"
        className="border-t border-black/10 px-3 py-1 text-[10px] font-mono text-red-700 dark:text-red-300 flex items-center gap-1"
      >
        <span>✗</span>
        <span className="truncate" title={entry.error ?? ''}>
          {entry.error ?? t('node.status.errorGeneric')}
        </span>
      </div>
    )
  }
  return null
}

export const BaseNode = memo(function BaseNode({ type, label, selected, hasInput = true, hasOutput = true, children, nodeId }: Props) {
  const { visuals, manifest } = useNodeVisuals()
  // Walk `extends` so preset types (wikipedia / brave_search / …)
  // inherit their parent's visual. See nodeStyles.resolveVisual.
  // Falls back to a generic slate style if neither the type nor any
  // parent is registered — typically: a stale node type that has
  // since been removed from the manifest.
  const v = resolveVisual(type, visuals, manifest) ?? GENERIC_VISUAL
  const t = useT()
  const displayLabel = label || t(`nodes.${v.i18nKey}.label`)
  // Per-node trace status drives BOTH the selection-style accent
  // ring AND the status footer. We read both pieces of state from the
  // same store; a single re-run produces both `statuses[nodeId]` (a
  // status enum) and an entry in `entries` (full outcome).
  // Subscribing to both keeps them in sync.
  const status = useTraceStore((s) => (nodeId ? s.statuses[nodeId] : undefined))
  // Most-recent entry for this node — used by the footer for duration,
  // token count, and error message. `entries` is append-only and the
  // last match wins (a loop body that runs 3 times shows the latest).
  const latestEntry = useTraceStore((s) => {
    if (!nodeId) return null
    for (let i = s.entries.length - 1; i >= 0; i--) {
      if (s.entries[i].nodeId === nodeId) return s.entries[i]
    }
    return null
  })
  // Trace-driven "where is the workflow" affordance: borrow the
  // selection ring for the running / errored node. We don't apply it
  // to successful runs (those stay quiet — the footer carries the
  // outcome) and we don't apply it to plain idle nodes. The boolean
  // collapses both `selected` and trace-active into a single class
  // branch, so a node that's selected AND running just gets the same
  // styling — no double-up.
  const traceActive = status === 'running' || status === 'error'
  const highlighted = selected || traceActive
  // Read the "unreachable" state set by the canvas during a drag.
  // `useNodeId()` works inside any React Flow custom node; we use it
  // as the lookup key. The wrappers also pass `nodeId` as a prop —
  // prefer it because some callers (e.g. node-preview thumbnails) may
  // render BaseNode outside React Flow.
  const rfId = useNodeId()
  const lookupId = nodeId ?? rfId ?? undefined
  const nodeData = useNodeData(lookupId)
  const unreachable = Boolean(nodeData?.unreachable)
  const unreachableReason =
    (nodeData?.unreachableReason as string | undefined) ||
    t('canvas.connectionMode.unreachable.incompatible')
  return (
    <div
      data-testid="node-base"
      data-status={status ?? 'idle'}
      className={[
        'min-w-[180px] max-w-[260px] rounded-md border-2 transition relative',
        v.color,
        v.text,
        highlighted
          ? 'ring-[3px] ring-accent shadow-lg scale-[1.02]'
          : 'shadow-sm',
        unreachable ? 'opacity-40 cursor-not-allowed' : '',
      ].join(' ')}
      title={unreachable ? unreachableReason : undefined}
      data-unreachable={unreachable ? 'true' : undefined}
    >
      {hasInput && (
        <Handle
          type="target"
          position={Position.Left}
          className="!bg-ink-muted !w-2.5 !h-2.5"
        />
      )}
      <div className="flex items-center gap-2 px-3 py-1.5 border-b border-black/10 font-medium text-sm">
        <v.Icon className="opacity-90" />
        <span className="truncate flex-1">{displayLabel}</span>
      </div>
      {children && (
        <div className="px-3 py-2 text-xs opacity-80">{children}</div>
      )}
      <StatusFooter entry={latestEntry} status={status} t={t} />
      {hasOutput && (
        <Handle
          type="source"
          position={Position.Right}
          className="!bg-ink-muted !w-2.5 !h-2.5"
        />
      )}
    </div>
  )
})