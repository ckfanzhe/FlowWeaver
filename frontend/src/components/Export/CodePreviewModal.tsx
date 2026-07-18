/**
 * Modal for previewing (and lightly editing) the generated Python before download.
 * - The textarea is editable, so the user can tweak instructions/comments quickly.
 * - "Download" writes whatever's currently in the textarea, not the original.
 */
import { useEffect, useState } from 'react'
import { useT } from '../../i18n'

interface Props {
  open: boolean
  code: string
  filename: string
  loading?: boolean
  onClose: () => void
}

export function CodePreviewModal({ open, code: initialCode, filename, loading, onClose }: Props) {
  const t = useT()
  const [code, setCode] = useState(initialCode)
  const [copied, setCopied] = useState(false)

  // Reset the editable buffer each time the modal opens with fresh content.
  useEffect(() => {
    if (open) setCode(initialCode)
  }, [open, initialCode])

  // Esc to close.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  const download = () => {
    const blob = new Blob([code], { type: 'text/x-python' })
    const url = URL.createObjectURL(blob)
    try {
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      a.remove()
    } finally {
      URL.revokeObjectURL(url)
    }
  }

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      /* clipboard not available */
    }
  }

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/50">
      <div className="w-[80vw] max-w-4xl h-[80vh] rounded-lg bg-surface text-ink shadow-xl flex flex-col">
        <header className="flex items-center justify-between border-b border-edge px-4 py-3 flex-shrink-0">
          <h2 className="text-sm font-semibold truncate">
            <span className="text-ink-muted mr-2">{t('export.preview')}</span>
            <span className="font-mono">{filename}</span>
          </h2>
          <button
            className="rounded p-1 text-ink-muted hover:bg-surface-2"
            onClick={onClose}
            aria-label={t('common.close')}
            title={t('common.close')}
          >
            ✕
          </button>
        </header>

        <div className="flex-1 p-4 min-h-0">
          {loading ? (
            <div className="flex h-full items-center justify-center text-sm text-ink-muted">
              {t('export.generating')}
            </div>
          ) : (
            <textarea
              className="w-full h-full resize-none rounded border border-edge bg-surface p-3 font-mono text-xs leading-relaxed text-ink placeholder:text-ink-faint focus:outline-none focus:ring-1 focus:ring-accent"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              spellCheck={false}
            />
          )}
        </div>

        <footer className="flex items-center justify-between border-t border-edge px-4 py-3 flex-shrink-0">
          <div className="text-[11px] text-ink-muted">{t('export.editHint')}</div>
          <div className="flex gap-2">
            <button
              className="btn"
              onClick={copy}
              disabled={loading}
            >
              {copied ? t('common.copied') : t('common.copy')}
            </button>
            <button
              className="btn"
              onClick={onClose}
            >
              {t('common.cancel')}
            </button>
            <button
              className="btn-primary"
              onClick={download}
              disabled={loading}
            >
              {t('common.download')}
            </button>
          </div>
        </footer>
      </div>
    </div>
  )
}