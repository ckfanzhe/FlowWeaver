/**
 * Settings store — LLM presets + MCP servers.
 *
 * : per-user preferences (locale, theme) used to live here
 * too. They now belong on the user row, owned by `useIdentityStore`
 * and persisted to the backend via `/users/identify`. This module
 * is now strictly about the platform-wide resource CRUD that the
 * Settings drawer edits.
 */
import { create } from 'zustand'
import { llmPresetsApi } from '../api/llmPresets'
import { mcpServersApi } from '../api/mcpServers'
import type {
  LlmPreset,
  LlmPresetCreate,
  McpServerConfig,
} from '../types/workflow'

interface State {
  presets: LlmPreset[]
  mcpServers: McpServerConfig[]
  loading: boolean
  error: string | null

  // ─ UI shell state (lives here so any component can open Settings) ─
  settingsOpen: boolean
}

interface Actions {
  refresh: () => Promise<void>
  // LLM preset CRUD
  createPreset: (body: LlmPresetCreate) => Promise<LlmPreset>
  updatePreset: (id: string, body: Partial<LlmPresetCreate>) => Promise<void>
  deletePreset: (id: string) => Promise<void>
  setDefaultPreset: (id: string) => Promise<void>
  // MCP server CRUD
  createMcpServer: (body: Omit<McpServerConfig, 'id'>) => Promise<McpServerConfig>
  updateMcpServer: (id: string, body: Partial<McpServerConfig>) => Promise<void>
  deleteMcpServer: (id: string) => Promise<void>
  // Settings drawer
  openSettings: () => void
  closeSettings: () => void
}

export const useSettingsStore = create<State & Actions>((set, get) => ({
  presets: [],
  mcpServers: [],
  loading: false,
  error: null,

  settingsOpen: false,

  refresh: async () => {
    set({ loading: true, error: null })
    try {
      const [presets, mcpServers] = await Promise.all([
        llmPresetsApi.list(),
        mcpServersApi.list(),
      ])
      set({ presets, mcpServers, loading: false })
    } catch (e) {
      set({ error: (e as Error).message, loading: false })
    }
  },

  createPreset: async (body) => {
    const p = await llmPresetsApi.create(body)
    set((s) => ({ presets: [...s.presets, p] }))
    if (p.isDefault) {
      await get().refresh()
    }
    return p
  },

  updatePreset: async (id, body) => {
    const p = await llmPresetsApi.update(id, body)
    set((s) => ({
      presets: s.presets.map((x) => (x.id === id ? p : x)),
    }))
    if (body.isDefault) await get().refresh()
  },

  deletePreset: async (id) => {
    await llmPresetsApi.remove(id)
    set((s) => ({ presets: s.presets.filter((x) => x.id !== id) }))
  },

  setDefaultPreset: async (id) => {
    const p = await llmPresetsApi.setDefault(id)
    set((s) => ({
      presets: s.presets.map((x) => ({ ...x, isDefault: x.id === p.id })),
    }))
  },

  createMcpServer: async (body) => {
    const m = await mcpServersApi.create(body)
    set((s) => ({ mcpServers: [...s.mcpServers, m] }))
    return m
  },

  updateMcpServer: async (id, body) => {
    const m = await mcpServersApi.update(id, body)
    set((s) => ({
      mcpServers: s.mcpServers.map((x) => (x.id === id ? m : x)),
    }))
  },

  deleteMcpServer: async (id) => {
    await mcpServersApi.remove(id)
    set((s) => ({ mcpServers: s.mcpServers.filter((x) => x.id !== id) }))
  },

  openSettings: () => set({ settingsOpen: true }),
  closeSettings: () => set({ settingsOpen: false }),
}))
