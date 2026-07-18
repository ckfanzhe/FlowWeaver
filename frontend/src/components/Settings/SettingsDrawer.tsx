/**
 * Settings drawer — right-side panel for LLM presets and MCP servers.
 * Theme and language moved to the UserMenu (toolbar top-right) — see
 * `UserMenu.tsx`. The toolbar avatar now hosts the per-user
 * preferences so this drawer is purely about resource CRUD.
 * Triggered from the Toolbar "Settings" button.
 */
import { useEffect, useState } from 'react'
import { useSettingsStore } from '../../store/settingsStore'
import { useT } from '../../i18n'
import type {
  LlmPreset,
  LlmPresetCreate,
  McpServerConfig,
  McpTransport,
} from '../../types/workflow'
import { llmPresetsApi } from '../../api/llmPresets'

interface Props {
  open: boolean
  onClose: () => void
}

type Tab = 'llm' | 'mcp'

export function SettingsDrawer({ open, onClose }: Props) {
  const refresh = useSettingsStore((s) => s.refresh)
  const [tab, setTab] = useState<Tab>('llm')
  const t = useT()

  useEffect(() => {
    if (open) void refresh()
  }, [open, refresh])

  if (!open) return null

  return (
    <div className="fixed inset-0 z-30">
      <div
        className="absolute inset-0 bg-black/30"
        onClick={onClose}
        aria-hidden
      />
      <aside className="absolute right-0 top-0 h-full w-[480px] bg-surface shadow-xl flex flex-col">
        <header className="flex items-center justify-between border-b border-edge px-4 py-3">
          <h2 className="text-base font-semibold text-ink">{t('settings.title')}</h2>
          <button
            className="rounded p-1 text-ink-muted hover:bg-surface-2"
            onClick={onClose}
            aria-label={t('settings.close')}
            title={t('settings.close')}
          >
            ✕
          </button>
        </header>

        <nav className="flex border-b border-edge">
          {(['llm', 'mcp'] as Tab[]).map((tt) => (
            <button
              key={tt}
              className={[
                'flex-1 px-4 py-2 text-sm font-medium',
                tab === tt
                  ? 'border-b-2 border-accent text-accent-text'
                  : 'text-ink-muted hover:text-ink',
              ].join(' ')}
              onClick={() => setTab(tt)}
            >
              {t(`settings.tabs.${tt}`)}
            </button>
          ))}
        </nav>

        <div className="flex-1 overflow-y-auto p-4">
          {tab === 'llm' && <LlmTab />}
          {tab === 'mcp' && <McpTab />}
        </div>
      </aside>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// LLM tab
// ─────────────────────────────────────────────────────────────────
function LlmTab() {
  const presets = useSettingsStore((s) => s.presets)
  const { createPreset, updatePreset, deletePreset, setDefaultPreset } =
    useSettingsStore()
  const refresh = useSettingsStore((s) => s.refresh)
  const [editing, setEditing] = useState<LlmPreset | 'new' | null>(null)
  const t = useT()

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-xs text-ink-muted">{t('settings.llm.hint')}</p>
        <button
          className="btn-primary"
          onClick={() => setEditing('new')}
        >
          {t('settings.llm.add')}
        </button>
      </div>

      <ul className="space-y-2">
        {presets.length === 0 && (
          <li className="rounded border border-dashed border-edge px-3 py-6 text-center text-sm text-ink-muted">
            {t('settings.llm.noPresets')}
          </li>
        )}
        {presets.map((p) => (
          <li
            key={p.id}
            className="rounded-md border border-edge px-3 py-2 hover:bg-surface-2"
          >
            <div className="flex items-center justify-between">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate text-sm font-medium text-ink">{p.name}</span>
                  {p.thinking && (
                    <span
                      className="rounded bg-accent-soft px-1.5 py-0.5 text-[10px] font-medium text-accent-text"
                      title={t('settings.llm.thinkingOnHint')}
                      data-testid="preset-row-thinking-on"
                    >
                      {t('settings.llm.thinkingOn')}
                    </span>
                  )}
                </div>
                <div className="text-[11px] text-ink-muted font-mono">
                  {p.modelId}
                </div>
              </div>
              <div className="flex items-center gap-1">
                {/* Star button — always visible so the user can promote
                    any row to default. The single-default invariant is
                    enforced server-side: setting row B unsets every
                    other row in the same transaction. */}
                <button
                  className={[
                    'rounded px-2 py-1 text-[11px] hover:bg-surface-2',
                    p.isDefault ? 'text-accent-text' : 'text-ink-muted',
                  ].join(' ')}
                  onClick={() => {
                    if (p.isDefault) return  // already default — no-op
                    void setDefaultPreset(p.id)
                  }}
                  title={p.isDefault
                    ? t('settings.llm.currentDefault')
                    : t('settings.llm.setDefault')}
                  aria-pressed={p.isDefault}
                  data-testid="preset-row-star"
                >
                  {p.isDefault ? '★' : '☆'}
                </button>
                <button
                  className="rounded px-2 py-1 text-[11px] text-accent-text hover:bg-accent-soft"
                  onClick={async () => {
                    // P3 : re-query the preset before
                    // opening the editor so the form reflects the
                    // freshest row (covers the case where another tab
                    // updated the apiKey or thinking flag while this
                    // drawer was closed).
                    await refresh()
                    const fresh = useSettingsStore
                      .getState()
                      .presets.find((x) => x.id === p.id)
                    setEditing(fresh ?? p)
                  }}
                >
                  {t('common.edit')}
                </button>
                <button
                  className="rounded px-2 py-1 text-[11px] text-danger hover:bg-danger-bg"
                  onClick={() => {
                    if (confirm(t('settings.llm.deleteConfirm', { name: p.name }))) {
                      void deletePreset(p.id)
                    }
                  }}
                >
                  ✕
                </button>
              </div>
            </div>
          </li>
        ))}
      </ul>

      {editing && (
        // `key` forces a fresh mount per preset so React's useState
        // initializers always run with the right `initial`. Without it,
        // closing one preset and opening another could re-use stale
        // local state from the previous preset — the form would show
        // blank fields even though `initial` had the right values.
        <PresetForm
          key={editing === 'new' ? 'new' : editing.id}
          initial={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
          onSave={async (body) => {
            if (editing === 'new') {
              await createPreset(body)
            } else {
              await updatePreset(editing.id, body)
            }
            setEditing(null)
          }}
        />
      )}
    </div>
  )
}

function PresetForm({
  initial,
  onSave,
  onClose,
}: {
  initial: LlmPreset | null
  onSave: (body: LlmPresetCreate) => Promise<void>
  onClose: () => void
}) {
  const t = useT()
  const isEdit = initial != null
  // Local form state. Each setter is wrapped in `markDirty` so we
  // know which fields the user actually touched. On save we only
  // emit changed fields — see `onSave` below. This avoids the
  // "I changed one letter and accidentally overwrote my key"
  // footgun that the previous always-send-everything approach had.
  const [name, setNameState] = useState(initial?.name ?? '')
  const [provider, setProviderState] = useState(initial?.provider ?? 'anthropic')
  const [modelId, setModelIdState] = useState(initial?.modelId ?? '')
  const [apiKey, setApiKeyState] = useState('')
  const [baseUrl, setBaseUrlState] = useState(initial?.baseUrl ?? '')
  // P3 : per-preset thinking toggle. Default OFF so test
  // fixtures stay fast. Rendered as a button-style on/off switch
  // (NOT a checkbox) per the designer's note " button
  // ".
  const [thinking, setThinkingState] = useState(initial?.thinking ?? false)
  // P3 : track "is this preset the default one" so the form
  // header can show a `★ DEFAULT` badge. Mirrored from `initial` (the
  // prop the parent passed) and refreshed after the re-fetch below —
  // covers the case where another tab promoted a different preset
  // while this drawer was closed.
  const [isDefault, setIsDefaultState] = useState(!!initial?.isDefault)
  const [saving, setSaving] = useState(false)
  // Loading state for the re-fetch-on-open pass. We re-query the preset
  // via `GET /api/v1/llm-presets/{id}` whenever the form mounts so the
  // form opens against the freshest stored row (not whatever the
  // frontend store cached). The user explicitly asked for this in the
  // P3 review: "... ".
  const [loadingFresh, setLoadingFresh] = useState(isEdit)

  // P3 : on mount (and whenever `initial?.id` changes
  // mid-flight), re-fetch the preset from the server so the form shows
  // the latest persisted state. We only act when we have an id — for
  // a brand-new preset there's no row to fetch.
  useEffect(() => {
    if (!initial?.id) {
      setLoadingFresh(false)
      return
    }
    let cancelled = false
    setLoadingFresh(true)
    void llmPresetsApi
      .get(initial.id)
      .then((fresh) => {
        if (cancelled) return
        setNameState(fresh.name)
        setProviderState(fresh.provider)
        setModelIdState(fresh.modelId)
        setBaseUrlState(fresh.baseUrl ?? '')
        setThinkingState(!!fresh.thinking)
        setIsDefaultState(!!fresh.isDefault)
        setApiKeyState('')  // never echo back the raw key
      })
      .catch(() => {
        // network failure → keep whatever `initial` had
      })
      .finally(() => {
        if (!cancelled) setLoadingFresh(false)
      })
    return () => {
      cancelled = true
    }
  }, [initial?.id])

  // Dirty-tracking. Starts all-false for an edit (so untouched fields
  // stay untouched) and all-true for a new preset (everything must be
  // sent on create).
  const [dirty, setDirty] = useState<Record<string, boolean>>(() => {
    if (isEdit) return {} as Record<string, boolean>
    return {
      name: true, provider: true, modelId: true,
      apiKey: true, baseUrl: true, thinking: true,
    }
  })
  const mark = (k: string) => setDirty((d) => (d[k] ? d : { ...d, [k]: true }))
  const setName = (v: string) => { setNameState(v); mark('name') }
  const setProvider = (v: string) => { setProviderState(v); mark('provider') }
  const setModelId = (v: string) => { setModelIdState(v); mark('modelId') }
  const setApiKey = (v: string) => { setApiKeyState(v); mark('apiKey') }
  const setBaseUrl = (v: string) => { setBaseUrlState(v); mark('baseUrl') }
  const setThinking = (v: boolean) => { setThinkingState(v); mark('thinking') }

  // Creating a brand-new preset REQUIRES an apiKey — anything in the
  // list is assumed to be usable, and the runtime refuses to build a
  // model from a keyless row. Editing an existing preset keeps the
  // apiKey optional: the user might just be fixing the model_id or
  // base URL, and the backend preserves the stored key when the
  // PATCH payload omits `apiKey` (see `llm_presets.py`'s
  // "empty string clears / missing field leaves unchanged" rule).
  const apiKeyRequired = !isEdit

  // On edit, always show the apiKey field as "xxx" placeholder when a
  // key is stored — the backend never returns the raw value.
  const apiKeyPlaceholder = initial?.hasApiKey
    ? 'xxx'
    : t('settings.llm.placeholders.apiKey')

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/40">
      <div className="w-[420px] rounded-lg bg-surface p-5 shadow-xl">
        <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold text-ink">
          <span>
            {initial ? t('settings.llm.editPreset') : t('settings.llm.newPreset')}
            {loadingFresh && (
              <span className="ml-2 text-[10px] font-normal text-ink-muted">
                ({t('common.loading')})
              </span>
            )}
          </span>
          {/* P3 : `★ DEFAULT` chip inside the edit panel.
              The LlmTab list already shows a ★ on the default row, but
              when the user is mid-edit they can no longer see the list
              (the modal covers it). The chip is purely informational
              — toggling default happens in the list via the star
              button. The chip is hidden for new presets and for
              non-default presets. */}
          {initial && isDefault && (
            <span
              className="inline-flex items-center gap-1 rounded bg-accent-soft px-1.5 py-0.5 text-[10px] font-medium text-accent-text"
              title={t('settings.llm.currentDefault')}
              data-testid="preset-default-badge"
            >
              <span aria-hidden>★</span>
              {t('settings.llm.default')}
            </span>
          )}
        </h3>
        <div className="space-y-3">
          <Field label={t('settings.llm.fields.name')}>
            <input
              className="input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t('settings.llm.placeholders.name')}
            />
          </Field>
          <Field label={t('settings.llm.fields.provider')}>
            <select
              className="input"
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
            >
              <option value="openai">openai</option>
              <option value="anthropic">anthropic</option>
              <option value="ollama">ollama</option>
              <option value="google">google</option>
            </select>
          </Field>
          <Field label={t('settings.llm.fields.modelId')}>
            <input
              className="input"
              value={modelId}
              onChange={(e) => setModelId(e.target.value)}
              placeholder={t('settings.llm.placeholders.modelId')}
            />
          </Field>
          <Field label={t('settings.llm.fields.apiKey')}>
            <input
              type="password"
              className="input"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              required={apiKeyRequired}
              placeholder={apiKeyPlaceholder}
            />
          </Field>
          <Field label={t('settings.llm.fields.baseUrl')}>
            <input
              className="input"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder={t('settings.llm.placeholders.baseUrl')}
            />
          </Field>

          {/* P3 : per-preset thinking toggle. Button-style
              on/off so it's visually distinct from the provider /
              model dropdowns (which are selects). The active state
              uses accent-soft to mirror the accent chip used by the
              default badge. The default-preset setting was intentionally
              moved out of this form — see LlmTab's star button. */}
          <div className="flex items-start justify-between gap-3 pt-1">
            <div className="flex-1">
              <div className="field-label">{t('settings.llm.fields.thinking')}</div>
              <p className="text-[11px] text-ink-muted leading-snug">
                {t('settings.llm.fields.thinkingHint')}
              </p>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={thinking}
              onClick={() => setThinking(!thinking)}
              data-testid="preset-thinking-toggle"
              className={[
                'mt-1 inline-flex h-6 w-11 flex-shrink-0 items-center rounded-full transition-colors',
                thinking ? 'bg-accent' : 'bg-edge',
              ].join(' ')}
            >
              <span
                className={[
                  'inline-block h-4 w-4 transform rounded-full bg-surface transition-transform',
                  thinking ? 'translate-x-6' : 'translate-x-1',
                ].join(' ')}
              />
            </button>
          </div>
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <button
            className="btn"
            onClick={onClose}
          >
            {t('common.cancel')}
          </button>
          <button
            className="btn-primary"
            disabled={
              !name || !modelId || saving || (apiKeyRequired && !apiKey)
            }
            onClick={async () => {
              setSaving(true)
              try {
                // Emit ONLY the fields the user touched. For an edit
                // that didn't touch `apiKey`, omitting it tells the
                // backend to keep the stored key (see
                // `LlmPresetUpdate`'s "missing field leaves unchanged"
                // rule). The same applies to `thinking` — a user who
                // never flipped the toggle keeps the preset's existing
                // reasoning level.
                const body: Partial<LlmPresetCreate> = {}
                if (dirty.name) body.name = name
                if (dirty.provider) body.provider = provider
                if (dirty.modelId) body.modelId = modelId
                if (dirty.baseUrl) body.baseUrl = baseUrl
                if (dirty.thinking) body.thinking = thinking
                if (dirty.apiKey) {
                  if (apiKey || !isEdit) {
                    body.apiKey = apiKey
                  }
                  // If the user "touched" the apiKey field but left it
                  // empty AND we're editing, that's an explicit "clear
                  // my key" instruction — the backend interprets
                  // `apiKey=""` as clear. Because we use
                  // type="password" they can't accidentally leave it
                  // empty without typing something, so the
                  // `apiKey || !isEdit` guard covers the only legit
                  // create-time path. The "dirty but empty" branch on
                  // edit means "user opened the field but didn't type"
                  // — treat as no-op so we don't clobber the stored
                  // key.
                }
                await onSave(body as LlmPresetCreate)
              } finally {
                setSaving(false)
              }
            }}
          >
            {saving ? t('common.saving') : t('common.save')}
          </button>
        </div>
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// MCP tab
// ─────────────────────────────────────────────────────────────────
function McpTab() {
  const servers = useSettingsStore((s) => s.mcpServers)
  const { createMcpServer, updateMcpServer, deleteMcpServer } = useSettingsStore()
  const [editing, setEditing] = useState<McpServerConfig | 'new' | null>(null)
  const t = useT()

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-xs text-ink-muted">{t('settings.mcp.hint')}</p>
        <button
          className="btn-primary"
          onClick={() => setEditing('new')}
        >
          {t('settings.mcp.add')}
        </button>
      </div>

      <ul className="space-y-2">
        {servers.length === 0 && (
          <li className="rounded border border-dashed border-edge px-3 py-6 text-center text-sm text-ink-muted">
            {t('settings.mcp.noServers')}
          </li>
        )}
        {servers.map((s) => (
          <li
            key={s.id}
            className="rounded-md border border-edge px-3 py-2 hover:bg-surface-2"
          >
            <div className="flex items-center justify-between">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate text-sm font-medium text-ink">{s.name}</span>
                  <span className="rounded bg-surface-2 px-1.5 py-0.5 text-[10px] font-medium text-ink-muted">
                    {s.transport}
                  </span>
                  {!s.enabled && (
                    <span className="rounded bg-warning-bg px-1.5 py-0.5 text-[10px] text-warning">
                      {t('settings.mcp.disabled')}
                    </span>
                  )}
                </div>
                <div className="font-mono text-[11px] text-ink-muted truncate">
                  {s.transport === 'stdio' ? s.command : s.url}
                </div>
              </div>
              <div className="flex items-center gap-1">
                <button
                  className="rounded px-2 py-1 text-[11px] text-accent-text hover:bg-accent-soft"
                  onClick={() => setEditing(s)}
                >
                  {t('common.edit')}
                </button>
                <button
                  className="rounded px-2 py-1 text-[11px] text-danger hover:bg-danger-bg"
                  onClick={() => {
                    if (confirm(t('settings.mcp.deleteConfirm', { name: s.name }))) {
                      void deleteMcpServer(s.id)
                    }
                  }}
                >
                  ✕
                </button>
              </div>
            </div>
          </li>
        ))}
      </ul>

      {editing && (
        <McpForm
          initial={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
          onSave={async (body) => {
            if (editing === 'new') {
              await createMcpServer(body)
            } else {
              await updateMcpServer(editing.id, body)
            }
            setEditing(null)
          }}
        />
      )}
    </div>
  )
}

function McpForm({
  initial,
  onSave,
  onClose,
}: {
  initial: McpServerConfig | null
  onSave: (body: Omit<McpServerConfig, 'id'>) => Promise<void>
  onClose: () => void
}) {
  const t = useT()
  const [name, setName] = useState(initial?.name ?? '')
  const [transport, setTransport] = useState<McpTransport>(initial?.transport ?? 'stdio')
  const [enabled, setEnabled] = useState(initial?.enabled ?? true)
  const [command, setCommand] = useState(initial?.command ?? '')
  const [argsText, setArgsText] = useState((initial?.args ?? []).join(' '))
  const [url, setUrl] = useState(initial?.url ?? '')
  const [saving, setSaving] = useState(false)

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/40">
      <div className="w-[460px] rounded-lg bg-surface p-5 shadow-xl">
        <h3 className="mb-3 text-sm font-semibold text-ink">
          {initial ? t('settings.mcp.editServer') : t('settings.mcp.newServer')}
        </h3>
        <div className="space-y-3">
          <Field label={t('settings.mcp.fields.name')}>
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label={t('settings.mcp.fields.transport')}>
            <select
              className="input"
              value={transport}
              onChange={(e) => setTransport(e.target.value as McpTransport)}
            >
              <option value="stdio">{t('settings.mcp.transports.stdio')}</option>
              <option value="sse">{t('settings.mcp.transports.sse')}</option>
            </select>
          </Field>
          {transport === 'stdio' ? (
            <>
              <Field label={t('settings.mcp.fields.command')}>
                <input
                  className="input font-mono"
                  value={command}
                  onChange={(e) => setCommand(e.target.value)}
                  placeholder={t('settings.mcp.placeholders.command')}
                />
              </Field>
              <Field label={t('settings.mcp.fields.args')}>
                <input
                  className="input font-mono"
                  value={argsText}
                  onChange={(e) => setArgsText(e.target.value)}
                  placeholder={t('settings.mcp.placeholders.args')}
                />
              </Field>
            </>
          ) : (
            <Field label={t('settings.mcp.fields.url')}>
              <input
                className="input font-mono"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder={t('settings.mcp.placeholders.url')}
              />
            </Field>
          )}
          <label className="flex items-center gap-2 text-sm text-ink">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
            />
            {t('settings.mcp.fields.enabled')}
          </label>
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <button className="btn" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button
            className="btn-primary"
            disabled={!name || (transport === 'stdio' ? !command : !url) || saving}
            onClick={async () => {
              setSaving(true)
              try {
                const body: Omit<McpServerConfig, 'id'> = {
                  name,
                  transport,
                  enabled,
                }
                if (transport === 'stdio') {
                  body.command = command
                  body.args = argsText.split(/\s+/).filter(Boolean)
                } else {
                  body.url = url
                }
                await onSave(body)
              } finally {
                setSaving(false)
              }
            }}
          >
            {saving ? t('common.saving') : t('common.save')}
          </button>
        </div>
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Shared field
// ─────────────────────────────────────────────────────────────────
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <div className="field-label">{label}</div>
      {children}
    </label>
  )
}