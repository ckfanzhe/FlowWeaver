/**
 * Workflow list store — for the side panel where users browse saved workflows.
 * Also caches the gallery of built-in templates fetched once on app mount.
 */
import { create } from 'zustand'
import type { TemplateSummary, WorkflowSummary } from '../types/workflow'
import { workflowsApi } from '../api/workflows'

interface State {
  items: WorkflowSummary[]
  templates: TemplateSummary[]
  loading: boolean
  error: string | null
}

interface Actions {
  refresh: () => Promise<void>
  refreshTemplates: () => Promise<void>
  remove: (id: string) => Promise<void>
}

export const useWorkflowListStore = create<State & Actions>((set) => ({
  items: [],
  templates: [],
  loading: false,
  error: null,

  refresh: async () => {
    set({ loading: true, error: null })
    try {
      const items = await workflowsApi.list()
      set({ items, loading: false })
    } catch (e) {
      set({ error: (e as Error).message, loading: false })
    }
  },

  refreshTemplates: async () => {
    try {
      const templates = await workflowsApi.listTemplates()
      set({ templates })
    } catch (e) {
      // Templates list failure isn't fatal — the gallery just won't appear.
      // The Load menu and other UI paths don't depend on it.
      console.warn('refreshTemplates failed', e)
    }
  },

  remove: async (id) => {
    try {
      await workflowsApi.remove(id)
      set((s) => ({ items: s.items.filter((w) => w.id !== id) }))
    } catch (e) {
      set({ error: (e as Error).message })
    }
  },
}))