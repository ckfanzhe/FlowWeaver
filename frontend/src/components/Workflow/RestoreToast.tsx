/**
 * RestoreToast — shown on app boot when IndexedDB has a snapshot
 * newer than the backend's `updatedAt`. Two-button toast:
 *   • Restore  → applySnapshot (dirty=true) + delete snapshot
 *   • Discard  → delete snapshot, keep backend state
 * 6s auto-dismiss = treated as Discard (the safer default).
 *
 * Layout mirrors the share/import toast in `WorkflowToolbar.tsx`
 * (top-right, fixed, z-40) so the user sees one consistent surface
 * for transient confirmations.
 *
 * Positioning note: the toast portals at the top of <App> next to
 * the EmailGateModal — it must NOT be inside the toolbar (which
 * re-mounts on locale change), so it survives React's component tree
 * diffing cleanly.
 */
import { useEffect } from 'react'
import type { SnapshotEnvelope } from '../../lib/snapshotStore'

const AUTO_DISMISS_MS = 6000

function formatAge(ms: number): string {
  const ageMs = Date.now() - ms
  if (ageMs < 30_000) return 'just now'
  if (ageMs < 60_000) return 'less than a minute ago'
  const minutes = Math.floor(ageMs / 60_000)
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'} ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`
  const days = Math.floor(hours / 24)
  return `${days} day${days === 1 ? '' : 's'} ago`
}

export interface RestoreToastProps {
  snapshot: SnapshotEnvelope
  onRestore: () => void
  onDiscard: () => void
}

export function RestoreToast({ snapshot, onRestore, onDiscard }: RestoreToastProps) {
  // 6s auto-dismiss = Discard. matches SPEC P1 UX choice
  // (toast + Restore/Discard, non-blocking, default-discard).
  useEffect(() => {
    const timer = setTimeout(onDiscard, AUTO_DISMISS_MS)
    return () => clearTimeout(timer)
  }, [onDiscard])

  const age = formatAge(snapshot.savedAt)

  return (
    <div
      role="status"
      data-testid="restore-toast"
      className="fixed top-14 right-4 z-40 w-80 rounded-md border border-edge bg-surface px-3 py-2.5 shadow-lg"
    >
      <div className="text-sm text-ink leading-snug">
        Detected unsaved local changes from <span className="font-medium">{age}</span>.
      </div>
      <div className="mt-0.5 text-xs text-ink-muted truncate" title={snapshot.name}>
        “{snapshot.name || 'Untitled Workflow'}”
      </div>
      <div className="mt-2 flex items-center justify-end gap-2">
        <button
          type="button"
          className="btn !py-1 !px-2.5 !text-xs"
          onClick={onDiscard}
        >
          Discard
        </button>
        <button
          type="button"
          className="btn-primary !py-1 !px-2.5 !text-xs"
          onClick={onRestore}
          autoFocus
        >
          Restore
        </button>
      </div>
    </div>
  )
}