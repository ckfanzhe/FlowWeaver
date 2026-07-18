/**
 * Right-side drawer that renders a form for the currently-selected node.
 *
 * One component per node type to keep schemas tight — see `./AgentForm.tsx`,
 * `./RouterForm.tsx`, etc. This file is the outer shell: header + label
 * field + dispatch to the per-type form + delete button.
 *
 * Keyboard shortcut: Delete / Backspace is handled globally in App.tsx
 * (so the same shortcut works whether focus is on the canvas, the
 * panel, or anywhere else on the page).
 */
import type { NodeType } from '../../types/workflow'
import { useWorkflowStore } from '../../store/workflowStore'
import { useT } from '../../i18n'
import { resolveVisual, useNodeVisuals } from '../Nodes/nodeStyles'
import { useManifest } from '../../api/nodeTypes'

import { resolveForm } from './forms/registry'
import { Field } from './primitives'

interface Props {
  nodeId: string
}

export function PropertyPanel({ nodeId }: Props) {
  const node = useWorkflowStore((s) => s.nodes.find((n) => n.id === nodeId))
  const update = useWorkflowStore((s) => s.updateNodeData)
  const remove = useWorkflowStore((s) => s.removeNode)
  const selectNode = useWorkflowStore((s) => s.selectNode)
  const { visuals } = useNodeVisuals()
  const manifest = useManifest()
  const t = useT()

  if (!node) {
    return (
      <aside className="w-80 border-l border-edge bg-surface p-4 flex-shrink-0">
        <p className="text-sm text-ink-muted">{t('panel.empty')}</p>
      </aside>
    )
  }

  // Walk `extends` chain so preset types (wikipedia / brave_search /
  // open_meteo / …) inherit their parent's visual automatically. The
  // walk is bounded by `maxDepth` so a manifest cycle can't infinite-
  // loop here. If neither the type nor any parent is registered
  // (typically: a node type was removed from the manifest but the
  // workflow still references it), `v` is null and we render a
  // generic header instead of crashing on `v.i18nKey`.
  const v = resolveVisual(node.type as NodeType, visuals, manifest)
  const i18nKey = v?.i18nKey ?? (node.type as NodeType)
  const displayLabel = (node.data.label as string) || t(`nodes.${i18nKey}.label`)
  const FormComponent = resolveForm(node.type as NodeType, manifest)

  return (
    <aside className="w-80 border-l border-edge bg-surface flex-shrink-0 overflow-y-auto">
      <header className="px-4 py-3 border-b border-edge flex items-center gap-2">
        {v?.Icon ? <v.Icon className="opacity-90" /> : null}
        <div className="min-w-0">
          <div className="text-sm font-semibold text-ink">{displayLabel}</div>
          <div className="text-[10px] text-ink-faint font-mono">{node.id}</div>
        </div>
        <button
          className="ml-auto rounded p-1 text-ink-muted hover:bg-surface-2"
          onClick={() => selectNode(null)}
          title={t('panel.close')}
          aria-label={t('panel.close')}
        >
          ✕
        </button>
      </header>

      <div className="p-4 space-y-4">
        <Field label={t('panel.label')}>
          <input
            type="text"
            className="input"
            value={(node.data.label as string) ?? ''}
            onChange={(e) => update(node.id, { label: e.target.value })}
            placeholder={displayLabel}
          />
        </Field>

        {FormComponent
          ? <FormComponent nodeId={node.id} />
          : <NoConfigFallback />}
      </div>

      <footer className="px-4 py-3 border-t border-edge space-y-1">
        <button
          className="btn-danger w-full"
          onClick={() => remove(node.id)}
        >
          {t('panel.delete')}
        </button>
        <p className="text-center text-[10px] text-ink-faint font-mono">
          {t('panel.deleteHint')}
        </p>
      </footer>
    </aside>
  )
}

function NoConfigFallback() {
  const t = useT()
  return <p className="text-xs text-ink-muted">{t('panel.noConfig')}</p>
}