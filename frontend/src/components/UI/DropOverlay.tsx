/**
 * Full-screen overlay shown while the user is dragging a file over the
 * window. Rendered as a sibling at the top of the tree so it sits above
 * every other element (z-50 + backdrop-blur).
 *
 * Visual: a centered dashed-bordered card with the JSON icon and a
 * single-line hint. Uses currentColor so it adapts to dark/light mode.
 */
import { JsonIcon } from './Icons'

interface Props {
  /** i18n strings — supplied so this component stays presentational. */
  title: string
  hint?: string
}

export function DropOverlay({ title, hint }: Props) {
  return (
    <div
      aria-live="polite"
      className="fixed inset-0 z-50 flex items-center justify-center bg-bg/70 backdrop-blur-sm pointer-events-none"
    >
      <div className="flex flex-col items-center gap-3 rounded-2xl border-2 border-dashed border-accent bg-surface/80 px-12 py-10 shadow-2xl">
        <div className="text-accent">
          <JsonIcon className="w-12 h-12" />
        </div>
        <div className="text-lg font-semibold text-ink">{title}</div>
        {hint && <div className="text-sm text-ink-muted">{hint}</div>}
      </div>
    </div>
  )
}
