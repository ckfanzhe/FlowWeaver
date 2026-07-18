/**
 * Top-of-canvas toolbar: draggable node templates, Word-style.
 *
 * Mounted inside `WorkflowCanvas`, sitting flush above the
 * `ReactFlow` surface (see `WorkflowCanvas.tsx`). Each entry is
 * an icon + label chip; dragging it onto the canvas drops a new
 * node of the matching type. Two visual groups — Basic Nodes
 * (executable types) and Tools (tool_source types) — are
 * separated by a thin vertical divider, matching the convention
 * used by Word's Home tab (Clipboard · Font · Paragraph ·
 * Styles …).
 *
 * The bar is a fixed-height horizontal strip with `overflow-x-auto`:
 * if the viewport is too narrow for every chip + the group labels to
 * fit, the strip scrolls horizontally rather than wrapping or
 * growing vertically. Chips themselves never stretch — they stay at
 * their content width — so a narrow viewport never makes any single
 * node look "wider".
 */
import { useMemo } from 'react'
import type { DragEvent } from 'react'
import type { NodeType } from '../../types/workflow'
import { useT } from '../../i18n'
import { useNodeVisuals } from '../Nodes/nodeStyles'

export function NodePalette() {
  const t = useT()
  const { visuals, order } = useNodeVisuals()

  const onDragStart = (e: DragEvent<HTMLButtonElement>, type: NodeType) => {
    e.dataTransfer.setData('application/agnobuilder-node-type', type)
    e.dataTransfer.effectAllowed = 'move'
  }

  // Split the manifest's ordered list into the two visual groups. The
  // manifest's `category` (`executable` vs `tool_source`) is the
  // source of truth — keeping the split here in the consumer (rather
  // than reordering the manifest) means the per-node `paletteOrder`
  // still controls intra-group sort order.
  const { basics, tools } = useMemo(() => {
    const basics: NodeType[] = []
    const tools: NodeType[] = []
    for (const type of order) {
      const cat = visuals[type]?.category
      if (cat === 'tool_source') tools.push(type)
      else basics.push(type)
    }
    return { basics, tools }
  }, [order, visuals])

  return (
    <div
      data-testid="node-palette"
      role="toolbar"
      aria-label={t('palette.title')}
      className="flex h-9 items-center gap-0.5 border-b border-edge bg-surface px-2 overflow-x-auto flex-shrink-0"
    >
      <GroupLabel>{t('palette.basicNodes')}</GroupLabel>
      {basics.map((type) => (
        <PaletteChip
          key={type}
          type={type}
          visuals={visuals}
          t={t}
          onDragStart={onDragStart}
        />
      ))}
      {tools.length > 0 && <Divider />}
      {tools.length > 0 && <GroupLabel>{t('palette.tools')}</GroupLabel>}
      {tools.map((type) => (
        <PaletteChip
          key={type}
          type={type}
          visuals={visuals}
          t={t}
          onDragStart={onDragStart}
        />
      ))}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Building blocks
// ─────────────────────────────────────────────────────────────────

interface PaletteChipProps {
  type: NodeType
  visuals: ReturnType<typeof useNodeVisuals>['visuals']
  t: (key: string, vars?: Record<string, string | number>) => string
  onDragStart: (e: DragEvent<HTMLButtonElement>, type: NodeType) => string | void
}

/** One draggable node-template chip — icon + label, neutral button so
 *  the bar reads as a single toolbar row rather than a strip of
 *  coloured tiles. Hover the chip for the label tooltip; dragging
 *  starts the drop. */
function PaletteChip({ type, visuals, t, onDragStart }: PaletteChipProps) {
  const v = visuals[type]
  const label = t(`nodes.${v.i18nKey}.label`)
  return (
    <button
      type="button"
      draggable
      onDragStart={(e) => onDragStart(e, type)}
      title={label}
      aria-label={label}
      className="inline-flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-ink-muted hover:bg-surface-2 hover:text-ink focus:outline-none focus:ring-1 focus:ring-accent cursor-grab active:cursor-grabbing select-none whitespace-nowrap"
    >
      <v.Icon className="opacity-80 h-3.5 w-3.5" />
      <span>{label}</span>
    </button>
  )
}

/** Small uppercase label between groups — the Word "Clipboard",
 *  "Font" style group header. */
function GroupLabel({ children }: { children: React.ReactNode }) {
  return (
    <span className="shrink-0 text-[9px] font-semibold uppercase tracking-wider text-ink-faint px-1">
      {children}
    </span>
  )
}

/** Thin vertical separator, Word-style — visually breaks the
 *  chip-row into the same groups as the underlying category split. */
function Divider() {
  return (
    <span
      aria-hidden
      className="mx-1 h-4 w-px shrink-0 bg-edge"
    />
  )
}
