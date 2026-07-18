/**
 * TracePanel — side drawer showing per-node execution timeline.
 *
 * Reads from the trace store. Each entry shows:
 *   - status badge (running / ok / error)
 *   - label + node type
 *   - start time (ms since session start)
 *   - duration (filled when the node finishes)
 *   - tokens (input / output / total) for LLM-backed nodes
 *   - error message (if the node failed)
 *
 * Clicking an entry selects the corresponding node on the canvas.
 *
 * Per-node "where is the workflow right now" feedback lives on the
 * canvas itself (BaseNode applies a green halo for the running node
 * and a red halo for errored nodes; a status footer at the bottom of
 * each node carries the detailed outcome). This panel is now a
 * secondary read-only timeline rather than the primary surface.
 *
 * The previous "Re-run from here" feature was removed (P3 trace
 * overhaul): it was rarely used, and a partial re-run from the
 * middle of a workflow is a power-user flow that's better served by
 * a dedicated "Run from this node" control elsewhere — not bolted
 * onto a debug timeline.
 *
 * The drawer slides in from the right and is toggleable via the chat
 * panel header (so it doesn't crowd the canvas when not in use).
 */
import { useEffect, useRef } from 'react'
import { useTraceStore } from '../../store/traceStore'
import { useWorkflowStore } from '../../store/workflowStore'
import { useT } from '../../i18n'
import type { TraceEntry } from '../../store/traceStore'

interface Props {
  open: boolean
  onClose: () => void
}

export function TracePanel({ open, onClose }: Props) {
  const entries = useTraceStore((s) => s.entries)
  const totalTokens = useTraceStore((s) => s.totalTokens)
  const running = useTraceStore((s) => s.running)
  const selectNode = useWorkflowStore((s) => s.selectNode)
  const t = useT()
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [entries])

  if (!open) return null

  return (
    <aside
      className="fixed top-0 right-0 bottom-0 z-20 w-[360px] border-l border-edge bg-surface shadow-2xl flex flex-col"
      data-testid="trace-panel"
    >
      <header className="flex items-center justify-between border-b border-edge px-4 py-2">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold text-ink">{t('trace.title')}</h3>
          {running && (
            <span className="rounded bg-amber-100 dark:bg-amber-900/40 px-1.5 py-0.5 text-[10px] font-mono text-amber-700 dark:text-amber-300">
              {t('trace.running')}
            </span>
          )}
        </div>
        <button
          className="text-xs text-ink-muted hover:text-ink"
          onClick={onClose}
        >
          {t('trace.close')}
        </button>
      </header>

      <div ref={scrollRef} className="flex-1 overflow-y-auto px-3 py-3 space-y-2">
        {entries.length === 0 && (
          <p className="text-xs text-ink-faint text-center mt-8">
            {t('trace.empty')}
          </p>
        )}
        {entries.map((e, i) => (
          <TraceRow
            key={`${e.nodeId}-${i}`}
            entry={e}
            onSelect={() => selectNode(e.nodeId)}
          />
        ))}
      </div>

      <footer className="border-t border-edge px-4 py-2 text-[11px] font-mono text-ink-muted flex justify-between">
        <span>{t('trace.totalNodes', { count: entries.length })}</span>
        <span>
          {t('trace.tokens', {
            input: totalTokens.input,
            output: totalTokens.output,
            total: totalTokens.total,
          })}
        </span>
      </footer>
    </aside>
  )
}

interface RowProps {
  entry: TraceEntry
  onSelect: () => void
}

function TraceRow({ entry, onSelect }: RowProps) {
  const t = useT()
  const badge = badgeForStatus(entry.status, t)
  return (
    <div
      className={[
        'rounded-md border px-3 py-2 text-xs transition',
        badge.rowClass,
      ].join(' ')}
    >
      <div className="flex items-center gap-2">
        <span className={['inline-block rounded px-1.5 py-0.5 font-mono text-[10px]', badge.badgeClass].join(' ')}>
          {badge.label}
        </span>
        <button
          className="font-medium text-ink hover:underline flex-1 text-left truncate"
          onClick={onSelect}
          title={t('trace.clickToSelect')}
        >
          {entry.label}
        </button>
      </div>
      <div className="mt-1 grid grid-cols-3 gap-1 font-mono text-[10px] text-ink-muted">
        <div>{t('trace.start')}: +{entry.startedAt}ms</div>
        <div>
          {entry.durationMs !== null
            ? t('trace.duration', { ms: entry.durationMs })
            : t('trace.pending')}
        </div>
        <div>
          {entry.tokens
            ? t('trace.tokenCount', { count: entry.tokens.total })
            : t('trace.noTokens')}
        </div>
      </div>
      {entry.error && (
        <div className="mt-1 text-[10px] text-red-600 dark:text-red-400 truncate" title={entry.error}>
          {entry.error}
        </div>
      )}
    </div>
  )
}

function badgeForStatus(status: TraceEntry['status'], t: (k: string) => string): {
  label: string
  badgeClass: string
  rowClass: string
} {
  switch (status) {
    case 'running':
      return {
        label: t('trace.status.running'),
        badgeClass: 'bg-amber-200 text-amber-900 dark:bg-amber-700/40 dark:text-amber-200',
        rowClass: 'border-amber-300/60 bg-amber-50/50 dark:bg-amber-950/20',
      }
    case 'ok':
      return {
        label: t('trace.status.ok'),
        badgeClass: 'bg-emerald-200 text-emerald-900 dark:bg-emerald-700/40 dark:text-emerald-200',
        rowClass: 'border-emerald-300/40 bg-emerald-50/40 dark:bg-emerald-950/10',
      }
    case 'error':
      return {
        label: t('trace.status.error'),
        badgeClass: 'bg-red-200 text-red-900 dark:bg-red-700/40 dark:text-red-200',
        rowClass: 'border-red-300/60 bg-red-50/50 dark:bg-red-950/20',
      }
    default:
      return {
        label: status,
        badgeClass: 'bg-surface-2 text-ink-muted',
        rowClass: 'border-edge bg-surface',
      }
  }
}