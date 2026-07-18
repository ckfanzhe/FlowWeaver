/**
 * Tiny dropdown menu primitive — click trigger, click-outside to close,
 * keyboard (Esc) to close. Used by the toolbar overflow menu.
 */
import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'

export interface MenuItem {
  key: string
  label: ReactNode
  onClick: () => void
  disabled?: boolean
  /** Optional left-side icon component. */
  icon?: React.FC<{ className?: string }>
  /** Optional description under the label (e.g. workflow id). */
  description?: ReactNode
  /** Mark destructive items (red text). */
  destructive?: boolean
  /** Render a thin divider above this item. */
  dividerBefore?: boolean
}

interface Props {
  /** The button that opens the menu. */
  trigger: (props: { open: boolean }) => ReactNode
  items: MenuItem[]
  /** Right-align the panel under the trigger (default: true). */
  alignRight?: boolean
  /** Width class for the panel. */
  widthClass?: string
  /** Optional content rendered below the items (e.g. an inline list). */
  footer?: ReactNode
  /** Override the default `.btn` class on the trigger button. Use
   *  this when the trigger should blend into its row (no border /
   *  background) — the panel styling stays consistent either way. */
  triggerClassName?: string
}

export function DropdownMenu({
  trigger,
  items,
  alignRight = true,
  widthClass = 'w-56',
  footer,
  triggerClassName = 'btn',
}: Props) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        className={triggerClassName}
      >
        {trigger({ open })}
      </button>
      {open && (
        <div
          role="menu"
          className={[
            'absolute mt-1 rounded-md border border-edge bg-surface shadow-lg z-30 py-1',
            alignRight ? 'right-0' : 'left-0',
            widthClass,
          ].join(' ')}
        >
          {items.map((it) => {
            const Icon = it.icon
            return (
              <div key={it.key}>
                {it.dividerBefore && <div className="my-1 border-t border-edge" />}
                <button
                  role="menuitem"
                  disabled={it.disabled}
                  onClick={() => {
                    if (it.disabled) return
                    setOpen(false)
                    it.onClick()
                  }}
                className={[
                  'flex w-full items-start gap-2 px-3 py-1.5 text-sm hover:bg-surface-2 disabled:opacity-50 disabled:cursor-not-allowed text-left',
                  it.destructive ? 'text-danger' : 'text-ink',
                ].join(' ')}
              >
                {Icon && <Icon className="mt-0.5 opacity-80" />}
                <span className="min-w-0">
                  <span className="block leading-tight">{it.label}</span>
                  {it.description && (
                    <span className="block text-[10px] text-ink-faint leading-tight">
                      {it.description}
                    </span>
                  )}
                </span>
              </button>
              </div>
            )
          })}
          {footer && (
            <div className="mt-1 border-t border-edge pt-1 px-1 pb-1">{footer}</div>
          )}
        </div>
      )}
    </div>
  )
}
