/**
 * Form for the `flow` node — branches are wired via canvas edges;
 * this form surfaces the mode discriminator plus the HITL sub-form
 * (only effective in `sequential` mode).
 *
 * : replaces `ParallelForm` + `StepsForm`.
 * `mode` selects between concurrent fan-out (`parallel`) and
 * ordered pipeline (`sequential`). `requiresConfirmation` is a
 * single HITL prompt that gates the whole block in sequential
 * mode — different from `Loop` which has per-iteration confirmation.
 */
import { useT } from '../../i18n'
import { Field, NodeDataField } from './primitives'

type FlowMode = 'parallel' | 'sequential'

export function FlowForm({ nodeId }: { nodeId: string }) {
  const t = useT()
  return (
    <>
      {/* Mode discriminator — `parallel` fans out concurrently;
          `sequential` runs branches in edge order. */}
      <NodeDataField<FlowMode> nodeId={nodeId} path={['mode']}>
        {(value, set) => (
          <Field label={t('panel.flow.modeLabel')}>
            <div className="flex items-center gap-3 text-xs text-ink">
              {(['parallel', 'sequential'] as const).map((m) => (
                <label key={m} className="flex items-center gap-1.5">
                  <input
                    type="radio"
                    name={`flow-mode-${nodeId}`}
                    value={m}
                    checked={(value ?? 'parallel') === m}
                    onChange={() => set(m)}
                  />
                  <span>{t(`panel.flow.mode.${m}`)}</span>
                </label>
              ))}
            </div>
          </Field>
        )}
      </NodeDataField>

      <p className="text-[11px] text-ink-muted leading-snug">
        {t('panel.flow.hint')}
      </p>

      {/* HITL — only effective in `sequential` mode. The label is
          always visible so the user knows the option exists; the
          placeholder text explains the no-op behaviour in `parallel`. */}
      <NodeDataField<FlowMode> nodeId={nodeId} path={['mode']}>
        {(mode) => (
          <details
            className="rounded border border-edge bg-surface-sunken/40"
            open={mode === 'sequential' ? undefined : false}
          >
            <summary className="cursor-pointer select-none px-3 py-2 text-xs font-medium text-ink">
              {t('panel.flow.hitlLabel')}
            </summary>
            <div className="space-y-3 px-3 pb-3 pt-1">
              <p className="text-[10px] text-ink-muted leading-snug">
                {mode === 'sequential'
                  ? t('panel.flow.hitlHint')
                  : t('panel.flow.hitlHintParallel')}
              </p>
              <NodeDataField<boolean> nodeId={nodeId} path={['requiresConfirmation']}>
                {(value, set) => (
                  <label className="flex items-center gap-2 text-xs text-ink-muted">
                    <input
                      type="checkbox"
                      className="rounded border-edge"
                      checked={!!value}
                      onChange={(e) => set(e.target.checked)}
                    />
                    <span>{t('panel.branch.requiresConfirmationLabel')}</span>
                  </label>
                )}
              </NodeDataField>
              <NodeDataField<string> nodeId={nodeId} path={['confirmationMessage']}>
                {(value, set) => (
                  <Field label={t('panel.branch.confirmationMessageLabel')}>
                    <input
                      type="text"
                      className="input text-xs"
                      value={value ?? ''}
                      onChange={(e) => set(e.target.value)}
                      placeholder={t('panel.branch.confirmationMessagePlaceholder')}
                    />
                  </Field>
                )}
              </NodeDataField>
            </div>
          </details>
        )}
      </NodeDataField>
    </>
  )
}