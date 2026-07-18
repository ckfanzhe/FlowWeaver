/**
 * App-wide UI state — flags for transient modals and the chat panel's
 * floating-window bounds.
 *
 * Merged from `uiStore.ts` (cross-cutting UI flags) and `chatUiStore.ts`
 * (chat-panel position/size). Both are pure UI concerns with no
 * protocol implications — they were split across two stores only
 * because the chat-panel localStorage persistence originally felt
 * "chat-specific". After SPEC .C consolidation we treat the
 * chat panel as one of many cross-cutting UI surfaces (alongside the
 * template gallery modal) so they share a single store.
 *
 * Fields:
 *   - `templatesOpen` — template gallery modal visibility. Opened from
 *     the toolbar's "New" button AND auto-opened by App.tsx on a fresh
 *     user's first visit.
 *   - `panelOpen`     — floating chat panel visibility (decoupled from
 *     the trace panel, which is its own drawer).
 *   - `position` / `size` — where the floating chat panel sits and how
 *     big it is. Persisted to localStorage under
 *     `agnobuilder.ui.chatPanel.bounds` (namespaced) so the user gets
 *     the same arrangement on reload.
 *
 * Mirrors the `settingsOpen` pattern in `settingsStore` — keep cross-
 * cutting UI state out of component-local state so a future caller can
 * drive any modal without prop-drilling.
 */
import { create } from 'zustand'

interface Point {
  x: number
  y: number
}

interface Size {
  w: number
  h: number
}

interface AppUiState {
  // Cross-cutting modal flags
  templatesOpen: boolean

  // Chat panel floating window
  panelOpen: boolean
  position: Point
  size: Size
}

interface AppUiActions {
  // Template gallery
  openTemplates: () => void
  closeTemplates: () => void

  // Chat panel
  togglePanel: () => void
  showPanel: () => void
  hidePanel: () => void
  setPanelPosition: (x: number, y: number) => void
  setPanelSize: (w: number, h: number) => void
  resetPanelBounds: () => void
}

const STORAGE_KEY = 'agnobuilder.ui.chatPanel.bounds'

const PANEL_DEFAULTS: { position: Point; size: Size } = {
  // Default to a sensible spot: bottom-right area, below the toolbar.
  // Callers should clamp against the actual viewport / palette width
  // at the moment they apply these — `position` is not pre-clamped.
  position: { x: 0, y: 0 },
  size: { w: 360, h: 520 },
}

interface PersistedShape {
  position: Point
  size: Size
}

function _loadPersisted(): { position: Point; size: Size } {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return PANEL_DEFAULTS
    const parsed = JSON.parse(raw) as Partial<PersistedShape>
    const position =
      parsed.position &&
      Number.isFinite(parsed.position.x) &&
      Number.isFinite(parsed.position.y)
        ? { x: parsed.position.x, y: parsed.position.y }
        : PANEL_DEFAULTS.position
    const size =
      parsed.size &&
      Number.isFinite(parsed.size.w) &&
      Number.isFinite(parsed.size.h) &&
      parsed.size.w > 0 &&
      parsed.size.h > 0
        ? { w: parsed.size.w, h: parsed.size.h }
        : PANEL_DEFAULTS.size
    return { position, size }
  } catch {
    // localStorage unavailable or value corrupted — fall back silently.
    return PANEL_DEFAULTS
  }
}

function _persist(state: { position: Point; size: Size }): void {
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ position: state.position, size: state.size }),
    )
  } catch {
    // Quota exceeded or storage disabled — skip silently.
  }
}

const initial = _loadPersisted()

export const useAppUiStore = create<AppUiState & AppUiActions>((set) => ({
  templatesOpen: false,
  panelOpen: false,
  position: initial.position,
  size: initial.size,

  openTemplates: () => set({ templatesOpen: true }),
  closeTemplates: () => set({ templatesOpen: false }),

  togglePanel: () => set((s) => ({ panelOpen: !s.panelOpen })),
  showPanel: () => set({ panelOpen: true }),
  hidePanel: () => set({ panelOpen: false }),
  setPanelPosition: (x, y) =>
    set((s) => {
      _persist({ position: { x, y }, size: s.size })
      return { position: { x, y } }
    }),
  setPanelSize: (w, h) =>
    set((s) => {
      _persist({ position: s.position, size: { w, h } })
      return { size: { w, h } }
    }),
  resetPanelBounds: () =>
    set(() => {
      _persist(PANEL_DEFAULTS)
      return { position: PANEL_DEFAULTS.position, size: PANEL_DEFAULTS.size }
    }),
}))