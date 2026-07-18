/**
 * Custom canvas context menu — replaces the browser's default menu on the
 * React Flow canvas so right-click actions are localized.
 *
 * Three flavours:
 *   - pane (empty canvas):   quick-add the v1 node types, fit view
 *   - node (right-click on a node): duplicate + delete
 *   - edge (right-click on an edge): delete
 *
 * Closes on Esc, on click outside, or after an action.
 */
import { useEffect, useMemo, useRef } from 'react'
import { useT } from '../../i18n'
import { useNodeVisuals } from '../Nodes/nodeStyles'
import type { NodeType } from '../../types/workflow'

export type CanvasContextKind = 'pane' | 'node' | 'edge'

interface Props {
  /** clientX / clientY from the right-click event */
  x: number
  y: number
  kind: CanvasContextKind
  onClose: () => void
  onAddNode: (type: NodeType) => void
  onDelete: () => void
  onDuplicate?: () => void
  onFitView: () => void
}

// Estimated menu footprint — used to clamp the position so the menu doesn't
// spill off the viewport at the bottom-right corner.
const MENU_ESTIMATED_W = 200
const MENU_ESTIMATED_H = 260

export function CanvasContextMenu({
  x, y, kind, onClose, onAddNode, onDelete, onDuplicate, onFitView,
}: Props) {
  const t = useT()
  const { visuals, order } = useNodeVisuals()
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    // mousedown fires before any item click; we still need to allow the
    // button's own click to land, so we only close when the target is outside
    // the menu.
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  const [px, py] = useMemo(() => {
    const maxX = window.innerWidth - MENU_ESTIMATED_W - 8
    const maxY = window.innerHeight - MENU_ESTIMATED_H - 8
    return [Math.min(x, maxX), Math.min(y, maxY)]
  }, [x, y])

  return (
    <div
      ref={ref}
      role="menu"
      className="fixed z-50 min-w-[180px] rounded-md border border-edge bg-surface text-ink shadow-lg py-1"
      style={{ top: py, left: px }}
      onContextMenu={(e) => e.preventDefault()}
    >
      {kind === 'pane' && (
        <>
          <div className="px-3 py-1 text-[10px] uppercase tracking-wider text-ink-faint">
            {t('contextMenu.addNode')}
          </div>
          {order.map((type) => {
            const v = visuals[type]
            return (
              <button
                key={type}
                role="menuitem"
                className="flex w-full items-center gap-2 px-3 py-1.5 text-sm hover:bg-surface-2"
                onClick={() => {
                  onAddNode(type)
                  onClose()
                }}
              >
                <v.Icon />
                <span>{t(`nodes.${v.i18nKey}.label`)}</span>
              </button>
            )
          })}
          <div className="my-1 border-t border-edge" />
          <button
            role="menuitem"
            className="w-full px-3 py-1.5 text-left text-sm hover:bg-surface-2"
            onClick={() => {
              onFitView()
              onClose()
            }}
          >
            {t('contextMenu.fitView')}
          </button>
        </>
      )}
      {kind === 'node' && (
        <>
          {onDuplicate && (
            <button
              role="menuitem"
              className="w-full px-3 py-1.5 text-left text-sm hover:bg-surface-2"
              onClick={() => {
                onDuplicate()
                onClose()
              }}
            >
              {t('contextMenu.duplicate')}
            </button>
          )}
          <button
            role="menuitem"
            className="w-full px-3 py-1.5 text-left text-sm text-danger hover:bg-danger-bg"
            onClick={() => {
              onDelete()
              onClose()
            }}
          >
            {t('contextMenu.delete')}
          </button>
        </>
      )}
      {kind === 'edge' && (
        <button
          role="menuitem"
          className="w-full px-3 py-1.5 text-left text-sm text-danger hover:bg-danger-bg"
          onClick={() => {
            onDelete()
            onClose()
          }}
        >
          {t('contextMenu.delete')}
        </button>
      )}
    </div>
  )
}