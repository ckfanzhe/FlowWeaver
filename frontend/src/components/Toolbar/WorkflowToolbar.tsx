/**
 * Top toolbar: workflow name on the left; then a single "New Workflow"
 * dropdown on the right.
 *
 *   ┌─────────────────────┐
 *   │ New Workflow     ▾  │   ← click to open the dropdown
 *   └─────────────────────┘
 *
 * The dropdown lists New · Export trio · Import JSON · recent
 * workflows. There is NO Save button (auto-save lives in
 * `workflowStore`) and NO Run button (the chat sidebar is always
 * visible and has its own Run/Build toggle). Settings lives in the
 * user avatar popover.
 */
import { useEffect, useRef, useState } from 'react'
import { useWorkflowStore } from '../../store/workflowStore'
import { useWorkflowListStore } from '../../store/workflowListStore'
import { useSettingsStore } from '../../store/settingsStore'
import { useAppUiStore } from '../../store/appUiStore'
import { useT } from '../../i18n'
import { SettingsDrawer } from '../Settings/SettingsDrawer'
import { CodePreviewModal } from '../Export/CodePreviewModal'
import { TemplateGalleryModal } from '../Templates/TemplateGalleryModal'
import { UserMenu } from '../Identity/UserMenu'
import { workflowsApi } from '../../api/workflows'
import { importJsonWorkflow } from '../../lib/importJsonWorkflow'
import { DropdownMenu, type MenuItem } from '../UI/DropdownMenu'
import {
  ChevronDown,
  CopyIcon,
  DownloadIcon,
  JsonIcon,
  NewIcon,
  TrashIcon,
  UploadIcon,
  WrenchIcon,
} from '../UI/Icons'

export function WorkflowToolbar() {
  const { workflowId, name, error, loadFromBackend } = useWorkflowStore()

  const { items, refresh, refreshTemplates, remove: removeWorkflow } = useWorkflowListStore()
  const refreshSettings = useSettingsStore((s) => s.refresh)
  // Settings drawer state is global (settingsStore) so any component
  // — notably PropertyPanel's "no default model" guard — can open it.
  const settingsOpen = useSettingsStore((s) => s.settingsOpen)
  const closeSettings = useSettingsStore((s) => s.closeSettings)
  const [exporting, setExporting] = useState(false)
  const [preview, setPreview] = useState<{ code: string; filename: string } | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  // Template gallery visibility lives in `useAppUiStore` so App.tsx can
  // auto-open it on a fresh user's first visit (no prop drilling).
  const templatesOpen = useAppUiStore((s) => s.templatesOpen)
  const openTemplates = useAppUiStore((s) => s.openTemplates)
  const closeTemplates = useAppUiStore((s) => s.closeTemplates)

  // Hidden <input type="file"> for JSON import.
  const fileInputRef = useRef<HTMLInputElement>(null)

  const t = useT()

  useEffect(() => {
    refresh()
  }, [refresh])

  useEffect(() => {
    refreshTemplates()
  }, [refreshTemplates])

  useEffect(() => {
    void refreshSettings()
  }, [refreshSettings])

  // Auto-clear toast after 3s.
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 3000)
    return () => clearTimeout(t)
  }, [toast])

  const handleNew = () => {
    // Open the template gallery regardless of dirty state — the modal
    // itself surfaces a "discarding unsaved changes" warning if needed.
    openTemplates()
  }

  const handleExportPython = async () => {
    if (!workflowId) return
    setExporting(true)
    try {
      const result = await workflowsApi.exportPython(workflowId, name)
      setPreview(result)
    } catch (e) {
      useWorkflowStore.setState({ error: (e as Error).message })
    } finally {
      setExporting(false)
    }
  }

  const handleExportJson = async () => {
    if (!workflowId) return
    try {
      const { filename, raw } = await workflowsApi.exportJson(workflowId, name)
      _downloadBlob(new Blob([raw], { type: 'application/json' }), filename)
    } catch (e) {
      useWorkflowStore.setState({ error: (e as Error).message })
    }
  }

  const handleCopyJson = async () => {
    if (!workflowId) return
    try {
      const { raw } = await workflowsApi.exportJson(workflowId, name)
      await navigator.clipboard.writeText(raw)
      setToast(t('toolbar.share.copiedBody'))
    } catch (e) {
      setToast(t('toolbar.share.copyFailed', { error: (e as Error).message }))
    }
  }

  const handleImportClick = () => {
    fileInputRef.current?.click()
  }

  const handleFileChosen = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = '' // reset so the same file can be picked again
    if (!file) return
    const result = await importJsonWorkflow(file, t)
    if (result.ok && result.name) {
      setToast(t('toolbar.share.importOk', { name: result.name }))
    }
  }

  // ─────────────────────────────────────────────────────────────────
  // Dropdown items — New · Export trio · Import JSON.
  // ─────────────────────────────────────────────────────────────────
  const menuItems: MenuItem[] = [
    {
      key: 'new',
      label: t('toolbar.menu.new'),
      description: t('toolbar.menu.newHint'),
      icon: WrenchIcon,
      onClick: handleNew,
    },
    {
      key: 'export-python',
      label: t('toolbar.menu.export'),
      description: t('toolbar.menu.exportHint'),
      icon: DownloadIcon,
      disabled: !workflowId,
      onClick: handleExportPython,
      dividerBefore: true,
    },
    {
      key: 'export-json',
      label: t('toolbar.menu.exportJson'),
      description: t('toolbar.menu.exportJsonHint'),
      icon: JsonIcon,
      disabled: !workflowId,
      onClick: handleExportJson,
    },
    {
      key: 'copy-json',
      label: t('toolbar.menu.copyJson'),
      description: t('toolbar.menu.copyJsonHint'),
      icon: CopyIcon,
      disabled: !workflowId,
      onClick: handleCopyJson,
    },
    {
      key: 'import-json',
      label: t('toolbar.menu.importJson'),
      description: t('toolbar.menu.importJsonHint'),
      icon: UploadIcon,
      onClick: handleImportClick,
      dividerBefore: true,
    },
  ]

  // Inline list of saved workflows, shown under the menu items.
  // Filter by name / id before slicing — the search input is
  // local state so the dropdown stays responsive while typing.
  const [search, setSearch] = useState('')
  const filtered = search.trim()
    ? items.filter(
        (w) =>
          w.name.toLowerCase().includes(search.toLowerCase()) ||
          w.id.toLowerCase().includes(search.toLowerCase()),
      )
    : items
  const recent = filtered.slice(0, 8)

  const footer = (
    <div className="px-1">
      <div className="flex items-center justify-between px-2 py-1">
        <span className="text-[10px] uppercase tracking-wider text-ink-faint">
          {t('toolbar.menu.load')}
        </span>
        <span className="text-[10px] text-ink-faint">{filtered.length}</span>
      </div>
      {items.length === 0 ? (
        <p className="px-2 py-1 text-xs text-ink-muted">{t('toolbar.menu.noWorkflows')}</p>
      ) : (
        <>
          {/* Search input — narrows the inline list by name / id.
              When empty, behaves identically to the pre-feature
              flow (just the most-recent 8). */}
          <div className="px-2 pb-1">
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={t('toolbar.menu.searchPlaceholder')}
              aria-label={t('toolbar.menu.searchPlaceholder')}
              className="input w-full text-xs"
            />
          </div>
          <ul className="max-h-64 overflow-y-auto">
            {recent.length === 0 ? (
              <li className="px-2 py-1 text-xs text-ink-muted">
                {t('toolbar.menu.noMatches')}
              </li>
            ) : (
              recent.map((w) => {
                const isCurrent = w.id === workflowId
                return (
                  <li
                    key={w.id}
                    className={[
                      'group flex items-center gap-1 rounded px-2 py-1 hover:bg-surface-2',
                      isCurrent ? 'bg-accent-soft' : '',
                    ].join(' ')}
                  >
                    <button
                      className="flex-1 min-w-0 text-left"
                      onClick={async () => {
                        await loadFromBackend(w.id)
                      }}
                    >
                      <div className="truncate text-sm text-ink">{w.name}</div>
                      <div className="truncate text-[10px] text-ink-faint font-mono">
                        {w.id}
                      </div>
                    </button>
                    <button
                      className="rounded p-1 text-ink-faint opacity-0 group-hover:opacity-100 hover:text-danger"
                      onClick={(e) => {
                        e.stopPropagation()
                        removeWorkflow(w.id)
                      }}
                      title={t('common.delete')}
                      aria-label={t('common.delete')}
                    >
                      <TrashIcon />
                    </button>
                  </li>
                )
              })
            )}
            {filtered.length > recent.length && (
              <li className="px-2 py-1 text-[10px] text-ink-faint">
                +{filtered.length - recent.length} more…
              </li>
            )}
          </ul>
        </>
      )}
    </div>
  )

  return (
    <>
      <header className="flex items-center justify-between gap-2 border-b border-edge bg-surface px-3 py-1.5">
        <div className="flex items-center gap-2 min-w-0 flex-1">
          <input
            className="min-w-0 flex-1 border-none bg-transparent text-base font-semibold text-ink focus:outline-none focus:ring-1 focus:ring-accent rounded px-1"
            value={name}
            onChange={(e) =>
              useWorkflowStore.setState({ name: e.target.value, dirty: true })
            }
            placeholder={t('common.untitled')}
            title={workflowId ? `${name} · ${workflowId}` : name}
          />
        </div>

        {error && (
          <div className="mx-2 text-sm text-danger truncate max-w-xs">{error}</div>
        )}

        <div className="flex items-center gap-2 flex-shrink-0">
          {/* Single primary dropdown: trigger label is "New Workflow"
              and the dropdown body lists every other action
              (export · import · recent list). Settings lives in the
              avatar popover (UserMenu) below. */}
          <DropdownMenu
            alignRight
            widthClass="w-72"
            trigger={({ open }) => (
              <span className="flex items-center gap-1.5">
                <NewIcon />
                <span>{t('toolbar.menu.new')}</span>
                <ChevronDown className={open ? 'rotate-180 transition' : 'transition'} />
              </span>
            )}
            items={menuItems}
            footer={footer}
          />

          <UserMenu />
        </div>
      </header>

      {/* Hidden file input for JSON import */}
      <input
        ref={fileInputRef}
        type="file"
        accept="application/json,.json"
        className="hidden"
        onChange={handleFileChosen}
      />

      {/* Tiny toast — only used for share/import confirmations. */}
      {toast && (
        <div
          role="status"
          className="fixed top-14 right-4 z-40 max-w-sm rounded-md border border-edge bg-surface px-3 py-2 text-sm shadow-lg"
        >
          {toast}
        </div>
      )}

      <SettingsDrawer open={settingsOpen} onClose={() => closeSettings()} />
      <CodePreviewModal
        open={!!preview}
        code={preview?.code ?? ''}
        filename={preview?.filename ?? ''}
        loading={exporting}
        onClose={() => setPreview(null)}
      />
      <TemplateGalleryModal
        open={templatesOpen}
        onClose={() => closeTemplates()}
      />
    </>
  )
}

// ─────────────────────────────────────────────────────────────────
// helpers
// ─────────────────────────────────────────────────────────────────
function _downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}
