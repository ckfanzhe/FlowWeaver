/**
 * Form for the `agent` node — preset picker + instructions + advanced
 * panel (phase.1 / P1.1).
 *
 * The model picker reads the preset list from `useSettingsStore` and
 * exposes a "use default" sentinel (`__default__`). When the user picks
 * a preset, we mirror its (provider, modelId) into the node config so
 * the runtime has all the info it needs even if the preset is later
 * deleted (defensive — though the runtime normally re-resolves by id).
 *
 * phase.1 / P1.1: the collapsible "Advanced" panel exposes 11
 * low-frequency fields (reasoning, retries, parser_model, hooks, …).
 * They're hidden by default so the common case (model + instructions)
 * stays one screen.
 */
import { useSettingsStore } from '../../store/settingsStore'
import { useWorkflowStore } from '../../store/workflowStore'
import { useT } from '../../i18n'
import type { ModelConfig } from '../../types/workflow'
import { Field, NodeDataField } from './primitives'

export function AgentForm({ nodeId }: { nodeId: string }) {
  const presets = useSettingsStore((s) => s.presets)
  const openSettings = useSettingsStore((s) => s.openSettings)
  const t = useT()
  // The default preset is the first row with `isDefault=true` (the API
  // returns the list sorted `is_default DESC, name ASC`). The platform
  // resolves "no per-node override" to this preset at runtime.
  const defaultPreset = presets.find((p) => p.isDefault) ?? null
  const hasAnyPreset = presets.length > 0
  // For the hooks <select>: only `tool` nodes with `source='function'`
  // are valid hook sources (per — `http` + `mcp` + `tools`
  // collapsed to `tool` with a `source` discriminator).
  const nodes = useWorkflowStore((s) => s.nodes)
  const toolsNodeOptions = nodes.filter((n) => {
    if (n.id === nodeId) return false
    if (n.type !== 'tool') return false
    const cfg = (n.data?.config ?? {}) as { source?: string }
    return cfg.source === 'function'
  })
  return (
    <>
      {/* ── Model picker (preset list from DB, default as first option) ── */}
      <NodeDataField<ModelConfig | undefined> nodeId={nodeId} path={['model']}>
        {(_model, setModel) => {
          const model = _model
          const selectedPresetId = model?.presetId ?? null
          const selectValue = selectedPresetId ?? '__default__'
          return (
            <Field label={t('panel.agent.model')}>
              <select
                className="input"
                value={selectValue}
                onChange={(e) => {
                  const v = e.target.value
                  if (v === '__default__') {
                    // "use system default" → drop the override entirely
                    setModel(undefined)
                    return
                  }
                  const p = presets.find((x) => x.id === v)
                  if (!p) return
                  setModel({
                    presetId: p.id,
                    provider: p.provider,
                    modelId: p.modelId,
                  })
                }}
              >
                {defaultPreset && (
                  <option value="__default__">
                    ★ {t('panel.agent.modelDefault')} — {defaultPreset.name}
                  </option>
                )}
                {presets.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                    {p.isDefault ? ' ★' : ''}
                  </option>
                ))}
              </select>
            </Field>
          )
        }}
      </NodeDataField>

      {/* Guard only when there's truly no model to fall back on. If the
          user has manually picked a preset, the workflow is runnable
          even without a system default — so the warning is suppressed. */}
      {!defaultPreset && !hasAnyPreset && (
        <NoDefaultModelGuard hasAnyPreset={false} onOpenSettings={openSettings} />
      )}

      {/* ── Instructions (always editable — value is data, not state) ── */}
      <NodeDataField<string> nodeId={nodeId} path={['instructions']}>
        {(instructions, setInstructions) => (
          <Field label={t('panel.fields.instructions')}>
            <textarea
              className="input min-h-[120px]"
              value={instructions ?? ''}
              onChange={(e) => setInstructions(e.target.value)}
              placeholder={t('panel.agent.instructionsPlaceholder')}
            />
          </Field>
        )}
      </NodeDataField>

      {/* ── Advanced panel (phase.1 / P1.1) ───────────────────────── */}
      <details className="rounded border border-edge bg-surface-sunken/40">
        <summary className="cursor-pointer select-none px-3 py-2 text-xs font-medium text-ink">
          {t('panel.agent.advancedLabel')}
        </summary>
        <div className="space-y-3 px-3 pb-3 pt-1">
          <p className="text-[10px] text-ink-muted leading-snug">
            {t('panel.agent.advancedHint')}
          </p>

          {/* systemMessage — separate from `instructions` (developer-tier) */}
          <NodeDataField<string> nodeId={nodeId} path={['systemMessage']}>
            {(value, set) => (
              <Field label={t('panel.agent.systemMessageLabel')}>
                <textarea
                  className="input min-h-[60px] text-xs"
                  value={value ?? ''}
                  onChange={(e) => set(e.target.value)}
                  placeholder={t('panel.agent.systemMessagePlaceholder')}
                />
              </Field>
            )}
          </NodeDataField>

          {/* reasoning — bool + optional reasoning_model */}
          <NodeDataField<boolean> nodeId={nodeId} path={['reasoning']}>
            {(value, set) => (
              <label className="flex items-center gap-2 text-xs text-ink-muted">
                <input
                  type="checkbox"
                  className="rounded border-edge"
                  checked={!!value}
                  onChange={(e) => set(e.target.checked)}
                />
                <span>{t('panel.agent.reasoningLabel')}</span>
              </label>
            )}
          </NodeDataField>
          <NodeDataField<ModelConfig | undefined> nodeId={nodeId} path={['reasoningModel']}>
            {(_model, setModel) => (
              <Field label={t('panel.agent.reasoningModelLabel')}>
                <select
                  className="input"
                  value={_model?.presetId ?? ''}
                  onChange={(e) => {
                    const v = e.target.value
                    if (!v) {
                      setModel(undefined)
                      return
                    }
                    const p = presets.find((x) => x.id === v)
                    if (!p) return
                    setModel({
                      presetId: p.id,
                      provider: p.provider,
                      modelId: p.modelId,
                    })
                  }}
                >
                  <option value="">{t('panel.agent.subModelNone')}</option>
                  {presets.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                      {p.isDefault ? ' ★' : ''}
                    </option>
                  ))}
                </select>
              </Field>
            )}
          </NodeDataField>

          {/* retries + delay_between_retries */}
          <div className="grid grid-cols-2 gap-2">
            <NodeDataField<number> nodeId={nodeId} path={['retries']}>
              {(value, set) => (
                <Field label={t('panel.agent.retriesLabel')}>
                  <input
                    type="number"
                    min={0}
                    max={10}
                    className="input"
                    value={value ?? 0}
                    onChange={(e) => {
                      const n = parseInt(e.target.value, 10)
                      set(Number.isFinite(n) && n >= 0 ? Math.min(n, 10) : 0)
                    }}
                  />
                </Field>
              )}
            </NodeDataField>
            <NodeDataField<number> nodeId={nodeId} path={['delayBetweenRetries']}>
              {(value, set) => (
                <Field label={t('panel.agent.delayBetweenRetriesLabel')}>
                  <input
                    type="number"
                    min={0}
                    max={60}
                    className="input"
                    value={value ?? 1}
                    onChange={(e) => {
                      const n = parseInt(e.target.value, 10)
                      set(Number.isFinite(n) && n >= 0 ? Math.min(n, 60) : 0)
                    }}
                  />
                </Field>
              )}
            </NodeDataField>
          </div>

          {/* tool_call_limit */}
          <NodeDataField<number | null> nodeId={nodeId} path={['toolCallLimit']}>
            {(value, set) => (
              <Field label={t('panel.agent.toolCallLimitLabel')}>
                <input
                  type="number"
                  min={1}
                  max={1000}
                  className="input"
                  value={value ?? ''}
                  placeholder={t('panel.agent.toolCallLimitPlaceholder')}
                  onChange={(e) => {
                    const raw = e.target.value
                    if (raw === '') {
                      set(null)
                      return
                    }
                    const n = parseInt(raw, 10)
                    set(Number.isFinite(n) && n >= 1 ? Math.min(n, 1000) : 1)
                  }}
                />
              </Field>
            )}
          </NodeDataField>

          {/* add_datetime_to_context */}
          <NodeDataField<boolean> nodeId={nodeId} path={['addDatetimeToContext']}>
            {(value, set) => (
              <label className="flex items-center gap-2 text-xs text-ink-muted">
                <input
                  type="checkbox"
                  className="rounded border-edge"
                  checked={!!value}
                  onChange={(e) => set(e.target.checked)}
                />
                <span>{t('panel.agent.addDatetimeToContextLabel')}</span>
              </label>
            )}
          </NodeDataField>

          {/* parser_model + parser_model_prompt */}
          <NodeDataField<ModelConfig | undefined> nodeId={nodeId} path={['parserModel']}>
            {(_model, setModel) => (
              <Field label={t('panel.agent.parserModelLabel')}>
                <select
                  className="input"
                  value={_model?.presetId ?? ''}
                  onChange={(e) => {
                    const v = e.target.value
                    if (!v) {
                      setModel(undefined)
                      return
                    }
                    const p = presets.find((x) => x.id === v)
                    if (!p) return
                    setModel({
                      presetId: p.id,
                      provider: p.provider,
                      modelId: p.modelId,
                    })
                  }}
                >
                  <option value="">{t('panel.agent.subModelNone')}</option>
                  {presets.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                      {p.isDefault ? ' ★' : ''}
                    </option>
                  ))}
                </select>
              </Field>
            )}
          </NodeDataField>
          <NodeDataField<string> nodeId={nodeId} path={['parserModelPrompt']}>
            {(value, set) => (
              <Field label={t('panel.agent.parserModelPromptLabel')}>
                <textarea
                  className="input min-h-[60px] text-xs"
                  value={value ?? ''}
                  onChange={(e) => set(e.target.value)}
                  placeholder={t('panel.agent.parserModelPromptPlaceholder')}
                />
              </Field>
            )}
          </NodeDataField>

          {/* pre_hooks / post_hooks — list of `tools` node IDs */}
          <NodeDataField<string[]> nodeId={nodeId} path={['preHooks']}>
            {(value, set) => (
              <Field label={t('panel.agent.preHooksLabel')}>
                <HooksMultiSelect
                  options={toolsNodeOptions}
                  value={value ?? []}
                  onChange={set}
                  placeholder={t('panel.agent.hooksPlaceholder')}
                />
              </Field>
            )}
          </NodeDataField>
          <NodeDataField<string[]> nodeId={nodeId} path={['postHooks']}>
            {(value, set) => (
              <Field label={t('panel.agent.postHooksLabel')}>
                <HooksMultiSelect
                  options={toolsNodeOptions}
                  value={value ?? []}
                  onChange={set}
                  placeholder={t('panel.agent.hooksPlaceholder')}
                />
              </Field>
            )}
          </NodeDataField>
        </div>
      </details>
    </>
  )
}

/**
 * Multi-select for hook refs. Each `tools` node's functions are added
 * as a single chip; the user can pick/unpick whole nodes. We use a
 * `<select multiple>` for the simplest UX — a true chip UI is overkill
 * for v1.
 */
function HooksMultiSelect({
  options,
  value,
  onChange,
  placeholder,
}: {
  options: Array<{ id: string; label?: string; type: string }>
  value: string[]
  onChange: (next: string[]) => void
  placeholder: string
}) {
  return (
    <select
      multiple
      className="input min-h-[80px] text-xs"
      value={value}
      onChange={(e) => {
        const selected = Array.from(e.target.selectedOptions).map((o) => o.value)
        onChange(selected)
      }}
    >
      {options.length === 0 && (
        <option disabled value="">
          {placeholder}
        </option>
      )}
      {options.map((opt) => (
        <option key={opt.id} value={opt.id}>
          {opt.label || opt.id} ({opt.type})
        </option>
      ))}
    </select>
  )
}

/**
 * Inline guard shown when no default LLM preset is configured. The
 * workflow can't run until the user picks one — intercept with a single
 * CTA that opens Settings directly so the fix is one click away.
 */
function NoDefaultModelGuard({
  hasAnyPreset,
  onOpenSettings,
}: {
  hasAnyPreset: boolean
  onOpenSettings: () => void
}) {
  const t = useT()
  return (
    <div
      className="rounded-md border border-warning/40 bg-warning-bg/60 px-3 py-3 text-[11px] text-warning leading-snug space-y-2"
      role="alert"
      data-testid="agent-no-default-guard"
    >
      <div className="font-semibold">⚠ {t('panel.agent.noDefaultTitle')}</div>
      <p>
        {hasAnyPreset
          ? t('panel.agent.noDefaultWithPresets')
          : t('panel.agent.noDefaultNoPresets')}
      </p>
      <button
        type="button"
        className="btn-primary !py-1 !px-2 !text-[11px]"
        onClick={onOpenSettings}
      >
        {t('panel.agent.openSettings')}
      </button>
    </div>
  )
}