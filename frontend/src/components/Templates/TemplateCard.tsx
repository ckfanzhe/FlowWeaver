/**
 * One template card in the gallery. Shows:
 *  - colored chips for the node types (via `useNodeVisuals()`)
 *  - the template's `name` and `description` (sourced from the JSON
 *    the JSON file declares — locale-tagged at seed time, so the
 *    gallery just renders what the backend returns verbatim and
 *    never goes through the i18n dict for card copy)
 *  - the `nodeCount` / `edgeCount` quick stats
 *
 * Click triggers `onPick(templateId)`. The "+ Start empty" pseudo-card
 * is rendered separately by the parent so the visual hierarchy is clear.
 */
import type { TemplateSummary } from '../../types/workflow'
import { useNodeVisuals } from '../Nodes/nodeStyles'
import { useT } from '../../i18n'

interface Props {
  template: TemplateSummary
  onPick: (id: string) => void
  busy?: boolean
}

export function TemplateCard({ template, onPick, busy }: Props) {
  const t = useT()
  const { visuals } = useNodeVisuals()
  // Title and description come straight from the backend — the JSON's
  // `name`/`description` fields ARE the localized strings. The i18n
  // dict only carries the chrome (header / footer / category labels
  // / node-type chip labels), not the per-template copy.
  const name = template.name
  const description = template.description ?? ""

  return (
    <button
      type="button"
      onClick={() => onPick(template.id)}
      disabled={busy}
      className={[
        'group flex flex-col items-stretch gap-3 rounded-lg border border-edge bg-surface-1 p-4 text-left',
        'hover:border-accent hover:bg-surface-2 transition',
        'disabled:opacity-50 disabled:cursor-not-allowed',
      ].join(' ')}
    >
      {/* chip row — shows which node types this template uses */}
      <div className="flex flex-wrap gap-1.5 min-h-[28px]">
        {template.nodeTypes.map((nt) => {
          const v = visuals[nt]
          return (
            <span
              key={nt}
              className={[
                'inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium border',
                v.color,
                v.text,
              ].join(' ')}
              title={t(`nodes.${nt}.label`)}
            >
              {t(`nodes.${nt}.label`)}
            </span>
          )
        })}
      </div>

      <div className="flex-1 min-h-0">
        <div className="text-sm font-semibold text-ink truncate">{name}</div>
        <div className="mt-1 text-xs text-ink-muted line-clamp-3">{description}</div>
      </div>

      <div className="flex items-center justify-between text-[10px] text-ink-faint">
        <span className="uppercase tracking-wider">
          {template.category ? t(`templates.categories.${template.category}`) : ''}
        </span>
        <span className="font-mono">
          {template.nodeCount} {t('templates.nodes')} · {template.edgeCount} {t('templates.edges')}
        </span>
      </div>
    </button>
  )
}
