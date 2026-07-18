/**
 * LLM preset API client.
 */
import { api } from './client'
import type { LlmPreset, LlmPresetCreate } from '../types/workflow'

export const llmPresetsApi = {
  list: () => api.get<LlmPreset[]>('/api/v1/llm-presets'),
  get: (id: string) => api.get<LlmPreset>(`/api/v1/llm-presets/${id}`),
  create: (body: LlmPresetCreate) => api.post<LlmPreset>('/api/v1/llm-presets', body),
  update: (id: string, body: Partial<LlmPresetCreate>) =>
    api.patch<LlmPreset>(`/api/v1/llm-presets/${id}`, body),
  remove: (id: string) => api.delete<void>(`/api/v1/llm-presets/${id}`),
  setDefault: (id: string) => api.post<LlmPreset>(`/api/v1/llm-presets/${id}/default`),
}
