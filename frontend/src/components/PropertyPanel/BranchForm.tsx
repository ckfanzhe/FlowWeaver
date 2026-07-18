/**
 * Form for the `branch` node — .
 *
 * Replaces the prior `router` + `condition` PropertyPanel forms.
 * The top-level `mode` discriminator (`switch` | `if-else`) selects
 * the editor subtree:
 *
 *   - `switch` — N-ary routing via `agno.Router(selector=...)`. Three
 *     selector modes (`function` | `cel` | `hitl`) — same as the
 *     prior RouterForm.
 *   - `if-else` — binary condition via `agno.Condition(evaluator=...)`.
 *     Three evaluator modes (`function` | `cel` | `literal`) + an
 *     optional HITL `requiresConfirmation` toggle — same as the
 *     prior ConditionForm. The "then" edge is read-only display; the
 *     "else" target is editable as a fallback for cases where the
 *     2nd edge was removed.
 *
 * Per-branch hints (`BranchTarget.condition`) are no longer read by
 * any built-in picker (since the picker is gone — phase.1 / P1.3).
 * They still parse at export time for back-compat.
 */
import { useT } from '../../i18n'
import type {
  BranchMode,
  ConditionEvaluatorMode,
  RouterSelectorMode,
} from '../../types/workflow'
import { useWorkflowStore } from '../../store/workflowStore'
import { Field, NodeDataField } from './primitives'

const BRANCH_MODES: BranchMode[] = ['switch', 'if-else']
const SELECTOR_MODES: RouterSelectorMode[] = ['function', 'cel', 'hitl']
const EVALUATOR_MODES: ConditionEvaluatorMode[] = ['function', 'cel', 'literal']

export function BranchForm({ nodeId }: { nodeId: string }) {
  const t = useT()

  return (
    <>
      <p className="text-[11px] text-ink-muted leading-snug">
        {t('panel.branch.hint')}
      </p>

      {/* ─── Top-level mode discriminator ─────────────────────── */}
      <NodeDataField<BranchMode> nodeId={nodeId} path={['mode']}>
        {(value, set) => {
          const current: BranchMode =
            value && BRANCH_MODES.includes(value) ? value : 'switch'
          return (
            <Field label={t('panel.branch.modeLabel')}>
              <select
                className="input"
                value={current}
                onChange={(e) => set(e.target.value as BranchMode)}
              >
                <option value="switch">{t('panel.branch.modeSwitch')}</option>
                <option value="if-else">{t('panel.branch.modeIfElse')}</option>
              </select>
              <p className="mt-1 text-[10px] text-ink-muted leading-snug">
                {t(`panel.branch.modeHint.${current}`)}
              </p>
            </Field>
          )
        }}
      </NodeDataField>

      {/* ─── Mode-specific editor subtree ─────────────────────── */}
      <NodeDataField<BranchMode> nodeId={nodeId} path={['mode']}>
        {(mode) => {
          const current: BranchMode =
            mode && BRANCH_MODES.includes(mode) ? mode : 'switch'
          return current === 'switch' ? (
            <SwitchEditor nodeId={nodeId} />
          ) : (
            <IfElseEditor nodeId={nodeId} />
          )
        }}
      </NodeDataField>
    </>
  )
}

/* ──────────────────────────────────────────────────────────────────
 * Switch-mode editor (N-ary routing — Router primitive)
 * ────────────────────────────────────────────────────────────────── */
function SwitchEditor({ nodeId }: { nodeId: string }) {
  const t = useT()
  return (
    <>
      {/* Selector mode dropdown */}
      <NodeDataField<RouterSelectorMode> nodeId={nodeId} path={['selector', 'mode']}>
        {(value, set) => {
          const current: RouterSelectorMode =
            value && SELECTOR_MODES.includes(value) ? value : 'function'
          return (
            <Field label={t('panel.branch.selectorModeLabel')}>
              <select
                className="input"
                value={current}
                onChange={(e) => set(e.target.value as RouterSelectorMode)}
              >
                <option value="function">{t('panel.branch.modeFunction')}</option>
                <option value="cel">{t('panel.branch.modeCel')}</option>
                <option value="hitl">{t('panel.branch.modeHitl')}</option>
              </select>
              <p className="mt-1 text-[10px] text-ink-muted leading-snug">
                {t(`panel.branch.selectorModeHint.${current}`)}
              </p>
            </Field>
          )
        }}
      </NodeDataField>

      {/* Per-selector-mode editor */}
      <NodeDataField<RouterSelectorMode> nodeId={nodeId} path={['selector', 'mode']}>
        {(mode) => {
          const current: RouterSelectorMode =
            mode && SELECTOR_MODES.includes(mode) ? mode : 'function'
          if (current === 'function') {
            return (
              <NodeDataField<string> nodeId={nodeId} path={['selector', 'expression']}>
                {(value, set) => (
                  <Field label={t('panel.branch.expressionLabel')}>
                    <textarea
                      rows={3}
                      className="input font-mono text-[11px]"
                      value={value ?? ''}
                      onChange={(e) => set(e.target.value)}
                      placeholder={t('panel.branch.expressionPlaceholder')}
                    />
                    <p className="mt-1 text-[10px] text-ink-muted leading-snug">
                      {t('panel.branch.expressionHint')}
                    </p>
                  </Field>
                )}
              </NodeDataField>
            )
          }
          if (current === 'cel') {
            return (
              <NodeDataField<string> nodeId={nodeId} path={['selector', 'expression']}>
                {(value, set) => (
                  <Field label={t('panel.branch.celLabel')}>
                    <input
                      type="text"
                      className="input font-mono text-xs"
                      value={value ?? ''}
                      onChange={(e) => set(e.target.value)}
                      placeholder={t('panel.branch.celPlaceholder')}
                    />
                    <p className="mt-1 text-[10px] text-ink-muted leading-snug">
                      {t('panel.branch.celHint')}
                    </p>
                  </Field>
                )}
              </NodeDataField>
            )
          }
          // hitl — prompt shown to the user
          return (
            <NodeDataField<string> nodeId={nodeId} path={['selector', 'fallbackMessage']}>
              {(value, set) => (
                <Field label={t('panel.branch.hitlPromptLabel')}>
                  <textarea
                    rows={2}
                    className="input text-xs"
                    value={value ?? ''}
                    onChange={(e) => set(e.target.value)}
                    placeholder={t('panel.branch.hitlPromptPlaceholder')}
                  />
                  <p className="mt-1 text-[10px] text-ink-muted leading-snug">
                    {t('panel.branch.hitlPromptHint')}
                  </p>
                </Field>
              )}
            </NodeDataField>
          )
        }}
      </NodeDataField>
    </>
  )
}

/* ──────────────────────────────────────────────────────────────────
 * If-else-mode editor (binary condition — Condition primitive)
 * ────────────────────────────────────────────────────────────────── */
function IfElseEditor({ nodeId }: { nodeId: string }) {
  const t = useT()
  const nodes = useWorkflowStore((s) => s.nodes)
  const edges = useWorkflowStore((s) => s.edges)
  const elseOptions = nodes.filter((n) => n.id !== nodeId)
  const thenTarget = edges.find((e) => e.source === nodeId)?.target

  return (
    <>
      {/* Evaluator mode dropdown */}
      <NodeDataField<ConditionEvaluatorMode> nodeId={nodeId} path={['evaluator', 'mode']}>
        {(value, set) => {
          const current: ConditionEvaluatorMode =
            value && EVALUATOR_MODES.includes(value) ? value : 'function'
          return (
            <Field label={t('panel.branch.evaluatorModeLabel')}>
              <select
                className="input"
                value={current}
                onChange={(e) => set(e.target.value as ConditionEvaluatorMode)}
              >
                <option value="function">{t('panel.branch.modeFunction')}</option>
                <option value="cel">{t('panel.branch.modeCel')}</option>
                <option value="literal">{t('panel.branch.modeLiteral')}</option>
              </select>
              <p className="mt-1 text-[10px] text-ink-muted leading-snug">
                {t(`panel.branch.evaluatorModeHint.${current}`)}
              </p>
            </Field>
          )
        }}
      </NodeDataField>

      {/* Per-evaluator-mode editor */}
      <NodeDataField<ConditionEvaluatorMode> nodeId={nodeId} path={['evaluator', 'mode']}>
        {(mode) => {
          const current: ConditionEvaluatorMode =
            mode && EVALUATOR_MODES.includes(mode) ? mode : 'function'
          if (current === 'literal') {
            return (
              <NodeDataField<string> nodeId={nodeId} path={['evaluator', 'expression']}>
                {(value, set) => (
                  <Field label={t('panel.branch.literalLabel')}>
                    <select
                      className="input"
                      value={(value ?? 'True').toLowerCase() === 'false' ? 'False' : 'True'}
                      onChange={(e) => set(e.target.value)}
                    >
                      <option value="True">True</option>
                      <option value="False">False</option>
                    </select>
                  </Field>
                )}
              </NodeDataField>
            )
          }
          if (current === 'cel') {
            return (
              <NodeDataField<string> nodeId={nodeId} path={['evaluator', 'expression']}>
                {(value, set) => (
                  <Field label={t('panel.branch.celLabel')}>
                    <input
                      type="text"
                      className="input font-mono text-xs"
                      value={value ?? ''}
                      onChange={(e) => set(e.target.value)}
                      placeholder={t('panel.branch.celPlaceholder')}
                    />
                    <p className="mt-1 text-[10px] text-ink-muted leading-snug">
                      {t('panel.branch.celHint')}
                    </p>
                  </Field>
                )}
              </NodeDataField>
            )
          }
          // function mode (default)
          return (
            <NodeDataField<string> nodeId={nodeId} path={['evaluator', 'expression']}>
              {(value, set) => (
                <Field label={t('panel.branch.expressionLabel')}>
                  <textarea
                    rows={3}
                    className="input font-mono text-[11px]"
                    value={value ?? ''}
                    onChange={(e) => set(e.target.value)}
                    placeholder={t('panel.branch.expressionPlaceholder')}
                  />
                  <p className="mt-1 text-[10px] text-ink-muted leading-snug">
                    {t('panel.branch.expressionHint')}
                  </p>
                </Field>
              )}
            </NodeDataField>
          )
        }}
      </NodeDataField>

      {thenTarget && (
        <Field label={t('panel.branch.thenTarget')}>
          <div className="input bg-surface-2 text-xs font-mono cursor-default">
            {thenTarget}
            <span className="opacity-50">
              {' '}
              ({(nodes.find((n) => n.id === thenTarget)?.data?.label as string) ?? thenTarget})
            </span>
          </div>
          <p className="mt-1 text-[10px] text-ink-muted leading-snug">
            {t('panel.branch.thenHint')}
          </p>
        </Field>
      )}

      <NodeDataField<string> nodeId={nodeId} path={['elseTarget']}>
        {(value, set) => (
          <Field label={t('panel.branch.elseTarget')}>
            <select
              className="input"
              value={value ?? ''}
              onChange={(e) => set(e.target.value)}
            >
              <option value="">{t('panel.branch.elseNone')}</option>
              {elseOptions.map((n) => (
                <option key={n.id} value={n.id}>
                  {(n.data?.label as string) || n.id} ({n.type})
                </option>
              ))}
            </select>
            <p className="mt-1 text-[10px] text-ink-muted leading-snug">
              {t('panel.branch.elseHint')}
            </p>
          </Field>
        )}
      </NodeDataField>

      {/* HITL — requires_confirmation */}
      <details className="rounded border border-border bg-surface-1 px-3 py-2">
        <summary className="cursor-pointer text-[11px] font-medium">
          {t('panel.branch.hitlLabel')}
        </summary>
        <div className="mt-2 space-y-2">
          <NodeDataField<boolean> nodeId={nodeId} path={['requiresConfirmation']}>
            {(value, set) => (
              <label className="flex items-center gap-2 text-[11px]">
                <input
                  type="checkbox"
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
    </>
  )
}