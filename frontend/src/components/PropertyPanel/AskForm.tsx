/**
 * Form for the `ask` node — prompt + input kind + choices.
 *
 * : renamed from `HumanInputForm`. The node
 * identity is now `ask`; kind is `control_flow` (was `executable`).
 * The form shape and runtime UX are unchanged.
 *
 * The input kind drives the runtime UX:
 *   - `text`     → free-text `input()`
 *   - `confirm`  → yes/no boolean
 *   - `choice`   → pick one of N strings
 *
 * For `choice` the form also shows a comma-separated list of choices.
 */
import { useT } from '../../i18n'
import type { AskConfig } from '../../types/workflow'
import { Field, NodeDataField } from './primitives'

export function AskForm({ nodeId }: { nodeId: string }) {
  const t = useT()
  return (
    <>
      <NodeDataField<string> nodeId={nodeId} path={['prompt']}>
        {(prompt, setPrompt) => (
          <Field label={t('panel.fields.prompt')}>
            <textarea
              className="input min-h-[80px]"
              value={prompt ?? ''}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder={t('panel.ask.promptPlaceholder')}
            />
          </Field>
        )}
      </NodeDataField>
      <NodeDataField<AskConfig['inputType']> nodeId={nodeId} path={['inputType']}>
        {(kind, setKind) => (
          <Field label={t('panel.fields.inputType')}>
            <select
              className="input"
              value={kind ?? 'text'}
              onChange={(e) => setKind(e.target.value as AskConfig['inputType'])}
            >
              <option value="text">{t('panel.inputType.text')}</option>
              <option value="confirm">{t('panel.inputType.confirm')}</option>
              <option value="choice">{t('panel.inputType.choice')}</option>
            </select>
          </Field>
        )}
      </NodeDataField>
      <NodeDataField<AskConfig['inputType']> nodeId={nodeId} path={['inputType']}>
        {(kind) =>
          kind === 'choice' ? (
            <NodeDataField<string[]> nodeId={nodeId} path={['choices']}>
              {(choices, setChoices) => (
                <Field label={t('panel.fields.branches')}>
                  <input
                    className="input"
                    type="text"
                    value={(choices ?? []).join(', ')}
                    placeholder={t('panel.ask.choicesPlaceholder')}
                    onChange={(e) =>
                      setChoices(
                        e.target.value
                          .split(',')
                          .map((s) => s.trim())
                          .filter(Boolean)
                      )
                    }
                  />
                </Field>
              )}
            </NodeDataField>
          ) : null
        }
      </NodeDataField>
    </>
  )
}