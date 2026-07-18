/**
 * Generic UI icons used by the toolbar and other top-level chrome.
 * All monochrome, currentColor, same viewBox as the node icons.
 */
import type { ReactNode } from 'react'

type Props = { className?: string }

function wrap(children: ReactNode, className?: string) {
  return (
    <svg
      className={className}
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  )
}

/** + new file */
export function NewIcon({ className }: Props) {
  return wrap(
    <>
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
      <path d="M14 3v5h5" />
      <path d="M12 11v6" />
      <path d="M9 14h6" />
    </>,
    className
  )
}

/** folder + down arrow */
export function LoadIcon({ className }: Props) {
  return wrap(
    <>
      <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
      <path d="M12 11v5" />
      <path d="M9 14l3 3 3-3" />
    </>,
    className
  )
}

/** down arrow into a tray */
export function DownloadIcon({ className }: Props) {
  return wrap(
    <>
      <path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" />
      <path d="M7 11l5 5 5-5" />
      <path d="M12 4v12" />
    </>,
    className
  )
}

/** gear */
export function SettingsIcon({ className }: Props) {
  return wrap(
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
    </>,
    className
  )
}

/** three vertical dots */
export function MoreIcon({ className }: Props) {
  return wrap(
    <>
      <circle cx="12" cy="5" r="1.25" fill="currentColor" stroke="none" />
      <circle cx="12" cy="12" r="1.25" fill="currentColor" stroke="none" />
      <circle cx="12" cy="19" r="1.25" fill="currentColor" stroke="none" />
    </>,
    className
  )
}

/** floppy disk (save) */
export function SaveIcon({ className }: Props) {
  return wrap(
    <>
      <path d="M5 3h11l3 3v15a0 0 0 0 1 0 0H5z" />
      <path d="M7 3v6h10V3" />
      <path d="M7 14h10v7H7z" />
    </>,
    className
  )
}

/** triangle play */
export function PlayIcon({ className }: Props) {
  return wrap(
    <>
      <path d="M7 5l12 7-12 7z" fill="currentColor" stroke="currentColor" />
    </>,
    className
  )
}

/** trash */
export function TrashIcon({ className }: Props) {
  return wrap(
    <>
      <path d="M3 6h18" />
      <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
      <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
      <path d="M10 11v6" />
      <path d="M14 11v6" />
    </>,
    className
  )
}

/** chevron down */
export function ChevronDown({ className }: Props) {
  return wrap(<path d="M6 9l6 6 6-6" />, className)
}

/** stacked squares (copy) */
export function CopyIcon({ className }: Props) {
  return wrap(
    <>
      <rect x="9" y="9" width="11" height="11" rx="1.5" />
      <path d="M5 15V5a2 2 0 0 1 2-2h10" />
    </>,
    className
  )
}

/** upload arrow into a tray */
export function UploadIcon({ className }: Props) {
  return wrap(
    <>
      <path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" />
      <path d="M7 11l5-5 5 5" />
      <path d="M12 4v12" />
    </>,
    className
  )
}

/** file with braces (json) */
export function JsonIcon({ className }: Props) {
  return wrap(
    <>
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h7" />
      <path d="M14 3v5h5" />
      <path d="M9 13c-.5-.5-1-1-1-1.5s.5-1 1-1.5" />
      <path d="M15 13c.5-.5 1-1 1-1.5s-.5-1-1-1.5" />
      <path d="M9 17c-.5.5-1 1-1 1.5" />
      <path d="M15 17c.5.5 1 1 1 1.5" />
    </>,
    className
  )
}

/** wrench — used for the "new workflow" toolbar entry; the wrench
 *  reads as "construct / build" (better fit for the builder canvas
 *  than the previous file-plus glyph, which suggested "new file"). */
export function WrenchIcon({ className }: Props) {
  return wrap(
    <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />,
    className
  )
}
