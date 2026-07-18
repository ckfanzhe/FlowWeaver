/**
 * MCP server API client.
 */
import { api } from './client'
import type { McpServerConfig } from '../types/workflow'

export const mcpServersApi = {
  list: () => api.get<McpServerConfig[]>('/api/v1/mcp-servers'),
  get: (id: string) => api.get<McpServerConfig>(`/api/v1/mcp-servers/${id}`),
  create: (body: Omit<McpServerConfig, 'id'>) =>
    api.post<McpServerConfig>('/api/v1/mcp-servers', body),
  update: (id: string, body: Partial<McpServerConfig>) =>
    api.patch<McpServerConfig>(`/api/v1/mcp-servers/${id}`, body),
  remove: (id: string) => api.delete<void>(`/api/v1/mcp-servers/${id}`),
}
