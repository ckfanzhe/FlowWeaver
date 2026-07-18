/**
 * Template gallery — opens when the user clicks "New".
 *
 * Layout:
 *   - Header: title + close button
 *   - Subtitle + (optional) dirty-canvas warning
 *   - Grid of 8 template cards
 *   - Top-right: "Start empty" card (the blank option)
 *
 * Behaviour:
 *   - On card click: POST /from-template/{id}, write the response into
 *     the workflow store via `applyTemplateResult`, close the modal,
 *     hide the chat panel.
 *   - On "Start empty": call `createNew('Untitled')`, close, hide.
 *   - Esc / overlay click closes without side effects.
 *   - Network errors surface inline (red banner) — modal stays open.
 *   - Gallery is filtered to the current i18n locale: the backend's
 *     `locale` field (per-template, sourced from the JSON's `locale`
 *     declaration) decides whether the card shows. When the user is
 *     in Chinese mode the gallery shows the `zh-CN` templates; in
 *     English mode it shows the `en` templates. No manual toggle —
 *     the gallery auto-follows the i18n locale the user already set.
 */
import { useEffect, useMemo, useState } from 'react'
import { getLocale, useT } from '../../i18n'
import { TemplateCard } from './TemplateCard'
import type { TemplateSummary } from '../../types/workflow'
import { workflowsApi } from '../../api/workflows'
import { useWorkflowListStore } from '../../store/workflowListStore'
import { useWorkflowStore } from '../../store/workflowStore'
import { useChatStore } from '../../store/chatStore'

interface Props {
  open: boolean
  onClose: () => void
}

/**
 * The locale the gallery filters by. Derived from the UI's i18n
 * locale (`getLocale()`) — when the user flips the platform language
 * the gallery flips with it. We only branch on `zh-CN` vs `en`; any
 * other i18n locale falls back to `en` so we never show an empty
 * gallery on a freshly-added locale.
 */
function galleryLocaleForCurrentUi(): 'zh-CN' | 'en' {
  return getLocale() === 'zh-CN' ? 'zh-CN' : 'en'
}

/**
 * Mark the user as having completed the first-run onboarding. Set
 * when they either pick a template OR choose "Start empty" from the
 * gallery. App.tsx reads this on mount to decide whether to auto-open
 * the gallery (only on a TRULY fresh visit).
 */
const ONBOARDED_KEY = 'agnobuilder.onboarded'

function markOnboarded(): void {
  try {
    localStorage.setItem(ONBOARDED_KEY, '1')
  } catch {
    /* localStorage may be unavailable — non-fatal */
  }
}

export function TemplateGalleryModal({ open, onClose }: Props) {
  const t = useT()
  const templates = useWorkflowListStore((s) => s.templates)
  const refreshTemplates = useWorkflowListStore((s) => s.refreshTemplates)
  const applyTemplateResult = useWorkflowStore((s) => s.applyTemplateResult)
  const createNew = useWorkflowStore((s) => s.createNew)
  const dirty = useWorkflowStore((s) => s.dirty)
  const hideChat = useChatStore((s) => s.hidePanel)

  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Filter the full template list by the UI's current i18n locale.
  // The backend stamps each row with the locale declared in the JSON
  // (`locale` field — added ).
  //
  // `useT()` subscribes to locale changes via `useSyncExternalStore`,
  // so this component re-renders on every locale switch (the `t`
  // declared at the top of the function does the subscribing). We
  // read `getLocale()` once per render and include it in the memo's
  // deps so the filter actually re-runs — without that dep the memo
  // would only invalidate when `templates` changed, and the gallery
  // would keep showing the previous locale's templates until the
  // user closed + reopened the modal (which used to be the only
  // thing that forced a re-mount).
  const currentGalleryLocale = galleryLocaleForCurrentUi()
  const visibleTemplates = useMemo(() => {
    return templates.filter((tpl) => (tpl.locale ?? 'en') === currentGalleryLocale)
  }, [templates, currentGalleryLocale])

  // Lazy-load the gallery on first open.
  useEffect(() => {
    if (open && templates.length === 0) {
      void refreshTemplates()
    }
  }, [open, templates.length, refreshTemplates])

  // Esc to close + reset transient state on close.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      setBusyId(null)
      setError(null)
    }
  }, [open, onClose])

  if (!open) return null

  const handlePickTemplate = async (id: string) => {
    setBusyId(id)
    setError(null)
    try {
      const wf = await workflowsApi.instantiateTemplate(id)
      applyTemplateResult(wf)
      refreshTemplates()  // refresh updated_at ordering, harmless
      hideChat()
      markOnboarded()
      onClose()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusyId(null)
    }
  }

  const handleStartEmpty = async () => {
    setBusyId('__blank__')
    setError(null)
    try {
      await createNew(t('toolbar.defaultName'))
      hideChat()
      markOnboarded()
      onClose()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusyId(null)
    }
  }

  const galleryLocale = galleryLocaleForCurrentUi()

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/50"
      onClick={onClose}
    >
      <div
        className="w-[min(96vw,1100px)] max-h-[88vh] rounded-lg bg-surface text-ink shadow-xl flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-start justify-between border-b border-edge px-5 py-4 flex-shrink-0">
          <div>
            <h2 className="text-base font-semibold">{t('templates.title')}</h2>
            <p className="mt-1 text-xs text-ink-muted">{t('templates.subtitle')}</p>
            {dirty && (
              <p className="mt-2 text-xs text-warning">
                {t('templates.discardWarning')}
              </p>
            )}
          </div>
          <button
            className="rounded p-1 text-ink-muted hover:bg-surface-2"
            onClick={onClose}
            aria-label={t('common.close')}
            title={t('common.close')}
          >
            ✕
          </button>
        </header>

        {error && (
          <div className="flex-shrink-0 border-b border-edge bg-danger-bg px-5 py-2 text-xs text-danger">
            {error}
          </div>
        )}

        <div className="flex-1 min-h-0 overflow-y-auto p-5">
          {visibleTemplates.length === 0 ? (
            <div className="flex h-32 items-center justify-center text-sm text-ink-muted">
              {templates.length === 0
                ? t('common.loading')
                : galleryLocale === 'zh-CN'
                  ? ''
                  : 'No templates available'}
            </div>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
              {visibleTemplates.map((tpl: TemplateSummary) => (
                <TemplateCard
                  key={tpl.id}
                  template={tpl}
                  onPick={handlePickTemplate}
                  busy={busyId === tpl.id}
                />
              ))}
            </div>
          )}
        </div>

        <footer className="flex items-center justify-between border-t border-edge px-5 py-3 flex-shrink-0">
          <span className="text-[11px] text-ink-faint">{t('templates.footer')}</span>
          <button
            className="btn-primary"
            onClick={handleStartEmpty}
            disabled={busyId !== null}
          >
            {t('templates.startEmpty')}
          </button>
        </footer>
      </div>
    </div>
  )
}