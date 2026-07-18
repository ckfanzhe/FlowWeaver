/**
 * SplitPane — left sidebar (chat) + right main pane (canvas).
 *
 * Two-pane layout:
 *   ┌────────────┬───┬──────────────────────────┐
 *   │  <sidebar> │ ÷ │  <main>                  │
 *   └────────────┴───┴──────────────────────────┘
 *       ← chatWidth → ← (100% − chatWidth − div) →
 *
 * Features:
 *   - Drag the divider to resize the sidebar between MIN and MAX.
 *   - Collapse button in the divider shrinks the sidebar to a thin
 *     re-expand rail. The collapsed state is a separate flag in
 *     localStorage so reloads preserve it.
 *   - Width + collapsed state persist to localStorage so the user
 *     gets the same arrangement on every page load.
 *   - Keyboard: double-click the divider to reset to DEFAULT.
 *
 * Why CSS-grid + percentage: percentages survive window resizes,
 * and the grid keeps the divider inside the layout so the main
 * pane width is always `100% − sidebar − divider` (no extra gap,
 * no leftover whitespace).
 *
 * Why React state drives the live drag (not DOM mutation):
 *   the earlier version mutated `sidebar.style.width` on every
 *   pointermove but kept the grid-template-columns at the OLD
 *   percentage. The two diverged and produced a visible blank
 *   strip until the next render. Driving the live width through
 *   React state makes the rendered columns always match the
 *   width the user sees; localStorage is only written on
 *   pointerup.
 */
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
} from 'react'

export const STORAGE_KEY = 'agnobuilder.splitPane.v1'

const DEFAULT_PCT = 42
const MIN_PCT = 18
const MAX_PCT = 70
const DIVIDER_PX = 6
const COLLAPSED_RAIL_PX = 28

interface PersistedShape {
  /** Sidebar width as a percentage of the layout container. */
  widthPct: number
  /** True when the sidebar is collapsed to the rail. */
  collapsed: boolean
}

function loadPersisted(): PersistedShape {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return { widthPct: DEFAULT_PCT, collapsed: false }
    const parsed = JSON.parse(raw) as Partial<PersistedShape>
    const widthPct =
      typeof parsed.widthPct === 'number' &&
      Number.isFinite(parsed.widthPct) &&
      parsed.widthPct >= MIN_PCT &&
      parsed.widthPct <= MAX_PCT
        ? parsed.widthPct
        : DEFAULT_PCT
    const collapsed = !!parsed.collapsed
    return { widthPct, collapsed }
  } catch {
    return { widthPct: DEFAULT_PCT, collapsed: false }
  }
}

function persist(s: PersistedShape): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(s))
  } catch {
    // Storage unavailable — non-fatal.
  }
}

export interface SplitPaneProps {
  /** Left pane (typically the chat sidebar). */
  sidebar: ReactNode
  /** Right pane (typically the canvas). */
  main: ReactNode
  /**
   * Optional test id for the outer container; lets tests scope
   * their queries without depending on CSS class names.
   */
  testId?: string
}

export function SplitPane({ sidebar, main, testId }: SplitPaneProps) {
  // Persisted layout (the source of truth across reloads).
  const [persisted, setPersisted] = useState<PersistedShape>(() => loadPersisted())

  // Live width during a drag. While idle this equals
  // `persisted.widthPct`; during a drag it tracks the cursor so
  // the grid template columns always match what the user sees.
  // We only write to localStorage on pointerup.
  const [liveWidthPct, setLiveWidthPct] = useState<number>(persisted.widthPct)
  const [dragging, setDragging] = useState(false)

  // Refs for the drag's start state — kept out of React state so
  // they don't trigger renders on every setup.
  const dragRef = useRef<{
    startX: number
    containerW: number
    origPct: number
  } | null>(null)

  const setCollapsed = useCallback((next: boolean) => {
    setPersisted((s) => {
      const merged = { ...s, collapsed: next }
      persist(merged)
      return merged
    })
  }, [])

  // Commit a final width — used both on pointerup and on
  // double-click reset.
  const commitWidthPct = useCallback((next: number) => {
    const clamped = Math.max(MIN_PCT, Math.min(MAX_PCT, next))
    setLiveWidthPct(clamped)
    setPersisted((s) => {
      const merged = { ...s, widthPct: clamped }
      persist(merged)
      return merged
    })
  }, [])

  // ── Drag handling ───────────────────────────────────────────
  const handleDividerPointerDown = useCallback(
    (e: ReactPointerEvent<HTMLDivElement>) => {
      if (persisted.collapsed) {
        // Click the divider in collapsed mode to re-expand.
        setCollapsed(false)
        e.preventDefault()
        return
      }
      e.preventDefault()
      // Capture the pointer so we keep getting move events even
      // if the cursor leaves the divider bounds.
      ;(e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId)
      const container = (e.currentTarget as HTMLElement).parentElement
      if (!container) return
      const rect = container.getBoundingClientRect()
      dragRef.current = {
        startX: e.clientX,
        containerW: rect.width,
        origPct: liveWidthPct,
      }
      setDragging(true)
    },
    [persisted.collapsed, liveWidthPct, setCollapsed],
  )

  useEffect(() => {
    if (!dragging) return
    const onMove = (ev: PointerEvent) => {
      const d = dragRef.current
      if (!d || d.containerW === 0) return
      const dx = ev.clientX - d.startX
      const nextPct = d.origPct + (dx / d.containerW) * 100
      setLiveWidthPct(Math.max(MIN_PCT, Math.min(MAX_PCT, nextPct)))
    }
    const onUp = () => {
      // Promote the live value to the persisted value via a
      // functional setter — `liveWidthPct` in the closure is the
      // value at the time the effect ran, not the latest.
      setLiveWidthPct((current) => {
        setPersisted((s) => {
          const merged = { ...s, widthPct: current }
          persist(merged)
          return merged
        })
        return current
      })
      dragRef.current = null
      setDragging(false)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
  }, [dragging])

  // CSS-grid columns: when collapsed, sidebar is the rail width;
  // otherwise it's `liveWidthPct%` and the divider is fixed. We
  // include the divider in the grid so it counts against the
  // total width — that keeps the main pane sized by the *visible*
  // sidebar rather than the sidebar + divider gap.
  const cols = persisted.collapsed
    ? `${COLLAPSED_RAIL_PX}px 0 1fr`
    : `${liveWidthPct}% ${DIVIDER_PX}px 1fr`
  const style: CSSProperties = {
    gridTemplateColumns: cols,
    cursor: dragging ? 'col-resize' : undefined,
    userSelect: dragging ? 'none' : undefined,
  }

  return (
    <div
      className="flex-1 min-h-0 grid"
      style={style}
      data-testid={testId ?? 'split-pane'}
    >
      <div
        className="flex flex-col min-w-0 min-h-0 overflow-hidden border-r border-edge bg-bg"
        data-testid="split-pane-sidebar"
      >
        {persisted.collapsed ? <CollapsedRail onExpand={() => setCollapsed(false)} /> : sidebar}
      </div>
      <DividerHandle
        collapsed={persisted.collapsed}
        dragging={dragging}
        onPointerDown={handleDividerPointerDown}
        onCollapse={() => setCollapsed(true)}
        onDoubleClick={() => commitWidthPct(DEFAULT_PCT)}
      />
      <div
        className="flex flex-col min-w-0 min-h-0 overflow-hidden"
        data-testid="split-pane-main"
      >
        {main}
      </div>
    </div>
  )
}

// ───────────────────────────────────────────────────────────────
// Collapsed rail — when the sidebar is folded, the user still
// needs a way to re-expand. We render a thin column with a single
// icon. The divider itself is also clickable in collapsed mode,
// but the rail gives a larger hit area.
// ───────────────────────────────────────────────────────────────
function CollapsedRail({ onExpand }: { onExpand: () => void }) {
  return (
    <button
      type="button"
      className="flex flex-col items-center justify-center h-full w-full text-ink-muted hover:text-ink hover:bg-surface-2"
      onClick={onExpand}
      title="Expand chat"
      aria-label="Expand chat"
      data-testid="split-pane-expand"
    >
      <ChevronRightIcon />
      <span
        className="text-[10px] font-mono mt-2 select-none"
        style={{ writingMode: 'vertical-rl' }}
      >
        Chat
      </span>
    </button>
  )
}

// ───────────────────────────────────────────────────────────────
// Divider — the drag handle. Renders as a thin track with a
// visible grip; the clickable region is the full 6px width.
// ───────────────────────────────────────────────────────────────
function DividerHandle({
  collapsed,
  dragging,
  onPointerDown,
  onCollapse,
  onDoubleClick,
}: {
  collapsed: boolean
  dragging: boolean
  onPointerDown: (e: ReactPointerEvent<HTMLDivElement>) => void
  onCollapse: () => void
  onDoubleClick: () => void
}) {
  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={collapsed ? 'Expand chat' : 'Resize chat'}
      className={[
        'flex flex-col items-center justify-center select-none',
        collapsed
          ? 'cursor-pointer bg-surface-2 hover:bg-surface-2/80'
          : dragging
            ? 'cursor-col-resize bg-surface-2'
            : 'cursor-col-resize bg-bg hover:bg-surface-2',
      ].join(' ')}
      style={{ width: collapsed ? 0 : undefined }}
      onPointerDown={onPointerDown}
      onDoubleClick={onDoubleClick}
      data-testid="split-pane-divider"
    >
      {!collapsed && (
        <>
          {/* Visible grip line */}
          <div className="w-px h-full bg-edge" />
          {/* Collapse button (rendered as an overlay so it gets a
              larger hit area than the divider itself). */}
          <button
            type="button"
            className="absolute -translate-y-1/2 top-1/2 z-10 rounded border border-edge bg-surface text-ink-muted hover:text-ink hover:bg-surface-2"
            style={{
              width: 16,
              height: 32,
              marginLeft: '-8px', // center over the divider
            }}
            onClick={(e) => {
              e.stopPropagation()
              onCollapse()
            }}
            onPointerDown={(e) => e.stopPropagation()}
            onDoubleClick={(e) => e.stopPropagation()}
            title="Collapse chat"
            aria-label="Collapse chat"
            data-testid="split-pane-collapse"
          >
            <ChevronLeftIcon />
          </button>
        </>
      )}
    </div>
  )
}

function ChevronLeftIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className="w-3 h-3 mx-auto"
      aria-hidden="true"
    >
      <path d="M15 18l-6-6 6-6" />
    </svg>
  )
}

function ChevronRightIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className="w-4 h-4"
      aria-hidden="true"
    >
      <path d="M9 18l6-6-6-6" />
    </svg>
  )
}