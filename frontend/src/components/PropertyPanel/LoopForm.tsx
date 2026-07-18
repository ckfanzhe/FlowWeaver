/**
 * Form for the `loop` node — body target + max iterations + end condition
 * + HITL panel (phase.1 / P1.5).
 *
 * The body target is the single step that gets re-executed each
 * iteration. End condition is a substring-match against the last
 * step's text — matches agno's native `Loop.end_condition` semantics.
 *
 * HITL panel exposes two independent checkpoints backed by
 * `Loop.human_review`:
 *   - requiresConfirmation: ask once before the loop starts.
 *   - requiresIterationReview: ask before each iteration.
 */
import { useWorkflowStore } from '../../store/workflowStore'
import { useT } from '../../i18n'
import { Field, NodeDataField } from './primitives'

export function LoopForm({ nodeId }: { nodeId: string }) {
  const t = useT()
  const nodes = useWorkflowStore((s) => s.nodes)
  // Possible body targets: any node other than the loop itself.
  const bodyOptions = nodes.filter((n) => n.id !== nodeId)
  return (
    <>
      <p className="text-[11px] text-ink-muted leading-snug">
        {t('panel.loop.hint')}
      </p>

      <NodeDataField<string> nodeId={nodeId} path={['bodyTarget']}>
        {(value, set) => (
          <Field label={t('panel.fields.bodyTarget')}>
            <select
              className="input"
              value={value ?? ''}
              onChange={(e) => set(e.target.value)}
            >
              <option value="">{t('panel.loop.bodyNone')}</option>
              {bodyOptions.map((n) => (
                <option key={n.id} value={n.id}>
                  {(n.data?.label as string) || n.id} ({n.type})
                </option>
              ))}
            </select>
            <p className="mt-1 text-[10px] text-ink-muted leading-snug">
              {t('panel.loop.bodyHint')}
            </p>
          </Field>
        )}
      </NodeDataField>

      <NodeDataField<number> nodeId={nodeId} path={['maxIterations']}>
        {(value, set) => (
          <Field label={t('panel.fields.maxIterations')}>
            <input
              type="number"
              min={1}
              max={100}
              className="input"
              value={value ?? 3}
              onChange={(e) => {
                const n = parseInt(e.target.value, 10)
                set(Number.isFinite(n) && n >= 1 ? n : 1)
              }}
            />
          </Field>
        )}
      </NodeDataField>

      <NodeDataField<string> nodeId={nodeId} path={['endCondition']}>
        {(value, set) => (
          <Field label={t('panel.fields.endCondition')}>
            <input
              type="text"
              className="input font-mono text-xs"
              value={value ?? ''}
              onChange={(e) => set(e.target.value)}
              placeholder={t('panel.loop.endConditionPlaceholder')}
            />
            <p className="mt-1 text-[10px] text-ink-muted leading-snug">
              {t('panel.loop.endConditionHint')}
            </p>
          </Field>
        )}
      </NodeDataField>

      <NodeDataField<boolean> nodeId={nodeId} path={['forwardIterationOutput']}>
        {(value, set) => (
          <label className="flex items-center gap-2 text-xs text-ink-muted">
            <input
              type="checkbox"
              className="rounded border-edge"
              checked={!!value}
              onChange={(e) => set(e.target.checked)}
            />
            <span>{t('panel.loop.forwardLabel')}</span>
          </label>
        )}
      </NodeDataField>

      {/* HITL panel (phase.1 / P1.5) */}
      <details className="rounded border border-edge bg-surface-sunken/40">
        <summary className="cursor-pointer select-none px-3 py-2 text-xs font-medium text-ink">
          {t('panel.loop.hitlLabel')}
        </summary>
        <div className="space-y-3 px-3 pb-3 pt-1">
          <p className="text-[10px] text-ink-muted leading-snug">
            {t('panel.loop.hitlHint')}
          </p>

          {/* requiresConfirmation — ask once before the loop starts */}
          <NodeDataField<boolean> nodeId={nodeId} path={['requiresConfirmation']}>
            {(value, set) => (
              <>
                <label className="flex items-center gap-2 text-xs text-ink-muted">
                  <input
                    type="checkbox"
                    className="rounded border-edge"
                    checked={!!value}
                    onChange={(e) => set(e.target.checked)}
                  />
                  <span>{t('panel.condition.requiresConfirmationLabel')}</span>
                </label>
                {value && (
                  <NodeDataField<string> nodeId={nodeId} path={['confirmationMessage']}>
                    {(msg, setMsg) => (
                      <Field label={t('panel.condition.confirmationMessageLabel')}>
                        <input
                          type="text"
                          className="input text-xs"
                          value={msg ?? ''}
                          onChange={(e) => setMsg(e.target.value)}
                          placeholder={t('panel.condition.confirmationMessagePlaceholder')}
                        />
                      </Field>
                    )}
                  </NodeDataField>
                )}
              </>
            )}
          </NodeDataField>

          {/* requiresIterationReview — ask before each iteration */}
          <NodeDataField<boolean> nodeId={nodeId} path={['requiresIterationReview']}>
            {(value, set) => (
              <>
                <label className="flex items-center gap-2 text-xs text-ink-muted">
                  <input
                    type="checkbox"
                    className="rounded border-edge"
                    checked={!!value}
                    onChange={(e) => set(e.target.checked)}
                  />
                  <span>{t('panel.loop.requiresIterationReviewLabel')}</span>
                </label>
                {value && (
                  <NodeDataField<string> nodeId={nodeId} path={['iterationReviewMessage']}>
                    {(msg, setMsg) => (
                      <Field label={t('panel.loop.iterationReviewMessageLabel')}>
                        <input
                          type="text"
                          className="input text-xs"
                          value={msg ?? ''}
                          onChange={(e) => setMsg(e.target.value)}
                          placeholder={t('panel.loop.iterationReviewMessagePlaceholder')}
                        />
                      </Field>
                    )}
                  </NodeDataField>
                )}
              </>
            )}
          </NodeDataField>
        </div>
      </details>
    </>
  )
}