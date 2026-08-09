/**
 * Form for the `knowledge` node — RAG / vector DB source.
 *
 * Parallel to `ToolForm` (mode-aware dispatcher). Three section layout:
 *   1. Identity + behaviour (`name`, `maxResults`, `addKnowledgeToContext`).
 *   2. pgvector config (DB URL / table name / schema).
 *   3. OpenAI embedder config (model id / API key / base URL).
 *   4. Sources list (`+ Add` row → path/url/text with optional reader).
 *
 * v1 ships a single hard-coded backend stack (locked 2026-08-25):
 *   - vectorDb: 'pgvector' — shares the docker-compose Postgres
 *     (`pgvector/pgvector:pg16`).
 *   - embedder: 'openai' — OpenAI / Azure / any OpenAI-compatible
 *     endpoint (vLLM, LocalAI) via `openaiBaseUrl`.
 *
 * The `vectorDb` + `embedder` discriminator fields are still written
 * to the store on every save (for forward-compat with the schema —
 * adding a future second backend widens the `Literal[...]` and the
 * picker is restored in one place). They are hidden in the UI today
 * (no picker); the canonical values `'pgvector'` and `'openai'` are
 * injected on mount.
 */
import { useEffect } from 'react'
import { useWorkflowStore } from '../../store/workflowStore'
import { useT } from '../../i18n'
import type { KnowledgeNodeConfig, KnowledgeSource } from '../../types/workflow'
import { Field, NodeDataField } from './primitives'

type SourceType = KnowledgeSource['type']

// v1 only — the discriminator is collapsed to a single value. Kept as
// a constant so the literal matches the schema's `Literal["pgvector"]`
// (a wider cast would widen to `string` otherwise).
const V1_VECTOR_DB = 'pgvector' as const
const V1_EMBEDDER = 'openai' as const

export function KnowledgeForm({ nodeId }: { nodeId: string }) {
  const t = useT()
  // Seed the store with the v1-only discriminator values on mount so
  // any export / downstream consumer sees a consistent `vectorDb` +
  // `embedder` even though the picker is hidden. (The form's previous
  // version had a `<select>` picker that lived in local state — v1
  // has no picker, so the only mutation is the mount-time seed.)
  useEffect(() => {
    const cfg = ((useWorkflowStore.getState().nodes.find((n) => n.id === nodeId)?.data?.config ?? {}) as Record<string, unknown>)
    const merged = structuredClone(cfg)
    let dirty = false
    if (merged.vectorDb !== V1_VECTOR_DB) {
      merged.vectorDb = V1_VECTOR_DB
      dirty = true
    }
    if (merged.embedder !== V1_EMBEDDER) {
      merged.embedder = V1_EMBEDDER
      dirty = true
    }
    if (dirty) {
      useWorkflowStore.getState().updateNodeData(nodeId, { config: merged })
    }
  }, [nodeId])

  const sources = useWorkflowStore((s) => {
    const n = s.nodes.find((nn) => nn.id === nodeId)
    return ((n?.data?.config as KnowledgeNodeConfig | undefined)?.sources ?? []) as KnowledgeSource[]
  })
  const update = useWorkflowStore((s) => s.updateNodeData)

  const setSources = (next: KnowledgeSource[]) => {
    const cfg = ((useWorkflowStore.getState().nodes.find((n) => n.id === nodeId)?.data?.config ?? {}) as Record<string, unknown>)
    const merged = structuredClone(cfg)
    merged.sources = next
    update(nodeId, { config: merged })
  }

  const addSource = () =>
    setSources([...sources, { type: 'text', value: '', reader: null }])

  const removeSource = (i: number) =>
    setSources(sources.filter((_, idx) => idx !== i))

  const updateSource = (i: number, patch: Partial<KnowledgeSource>) =>
    setSources(sources.map((s, idx) => (idx === i ? { ...s, ...patch } : s)))

  return (
    <div className="space-y-3">
      {/* ── Identity + behaviour ──────────────────────────────────── */}
      <NodeDataField<string>
        nodeId={nodeId}
        path={['name']}
      >
        {(value, set) => (
          <Field label={t('panel.knowledge.name')}>
            <input
              className="input"
              value={value ?? ''}
              onChange={(e) => set(e.target.value)}
              placeholder={t('panel.knowledge.namePlaceholder')}
            />
          </Field>
        )}
      </NodeDataField>

      <NodeDataField<number>
        nodeId={nodeId}
        path={['maxResults']}
      >
        {(value, set) => (
          <Field label={t('panel.knowledge.maxResults')}>
            <input
              className="input"
              type="number"
              min={1}
              max={100}
              value={value ?? 10}
              onChange={(e) => set(parseInt(e.target.value || '10', 10))}
            />
          </Field>
        )}
      </NodeDataField>

      <NodeDataField<boolean>
        nodeId={nodeId}
        path={['addKnowledgeToContext']}
      >
        {(value, set) => (
          <label className="flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={!!value}
              onChange={(e) => set(e.target.checked)}
            />
            {t('panel.knowledge.addKnowledgeToContext')}
          </label>
        )}
      </NodeDataField>

      {/* ── Vector DB — v1 ships pgvector only ─────────────────────── */}
      {/* The picker is hidden: the schema keeps the `vectorDb` field
          (Literal["pgvector"]) for forward-compat, but the UI never
          asks the user — `pgvector` is the only supported backend. */}
      <div className="rounded border border-teal-200/60 px-2 py-1 text-[11px] dark:border-teal-900">
        <span className="font-semibold">pgvector</span>
        <span className="opacity-60"> · {t('panel.knowledge.pgvectorBackendHint')}</span>
      </div>

      <NodeDataField<string> nodeId={nodeId} path={['pgvectorDbUrl']}>
        {(value, set) => (
          <Field label={t('panel.knowledge.pgvectorDbUrl')}>
            <input
              className="input font-mono text-[11px]"
              value={value ?? ''}
              onChange={(e) => set(e.target.value)}
              placeholder="postgresql://user:pass@host:5432/db"
            />
          </Field>
        )}
      </NodeDataField>
      <NodeDataField<string> nodeId={nodeId} path={['pgvectorTableName']}>
        {(value, set) => (
          <Field label={t('panel.knowledge.pgvectorTableName')}>
            <input
              className="input"
              value={value ?? 'agno_kb'}
              onChange={(e) => set(e.target.value)}
            />
          </Field>
        )}
      </NodeDataField>
      <NodeDataField<string> nodeId={nodeId} path={['pgvectorSchema']}>
        {(value, set) => (
          <Field label={t('panel.knowledge.pgvectorSchema')}>
            <input
              className="input"
              value={value ?? 'ai'}
              onChange={(e) => set(e.target.value)}
            />
          </Field>
        )}
      </NodeDataField>

      {/* ── Embedder — v1 ships OpenAI only ─────────────────────────── */}
      {/* Same forward-compat pattern: `embedder` discriminator field
          stays in the schema (Literal["openai"]) but the UI never
          asks — OpenAI is the only supported embedder. */}
      <div className="rounded border border-teal-200/60 px-2 py-1 text-[11px] dark:border-teal-900">
        <span className="font-semibold">openai</span>
        <span className="opacity-60"> · {t('panel.knowledge.openaiBackendHint')}</span>
      </div>

      <NodeDataField<string> nodeId={nodeId} path={['openaiModel']}>
        {(value, set) => (
          <Field label={t('panel.knowledge.openaiModel')}>
            <input
              className="input"
              value={value ?? 'text-embedding-3-small'}
              onChange={(e) => set(e.target.value)}
            />
          </Field>
        )}
      </NodeDataField>
      <NodeDataField<string> nodeId={nodeId} path={['openaiApiKey']}>
        {(value, set) => (
          <Field label={t('panel.knowledge.openaiApiKey')}>
            <input
              className="input font-mono text-[11px]"
              type="password"
              value={value ?? ''}
              onChange={(e) => set(e.target.value)}
              placeholder={t('panel.knowledge.apiKeyEnvFallback')}
            />
          </Field>
        )}
      </NodeDataField>
      <NodeDataField<string> nodeId={nodeId} path={['openaiBaseUrl']}>
        {(value, set) => (
          <Field label={t('panel.knowledge.openaiBaseUrl')}>
            <input
              className="input font-mono text-[11px]"
              value={value ?? ''}
              onChange={(e) => set(e.target.value)}
              placeholder="https://api.openai.com/v1"
            />
          </Field>
        )}
      </NodeDataField>

      {/* ── Sources ───────────────────────────────────────────────── */}
      <div>
        <div className="field-label">{t('panel.knowledge.sources')}</div>
        <div className="space-y-2">
          {sources.map((s, i) => (
            <div key={i} className="rounded border border-slate-200 p-2 dark:border-slate-700">
              <div className="flex items-center gap-2 mb-1">
                <select
                  className="input flex-1"
                  value={s.type ?? 'text'}
                  onChange={(e) =>
                    updateSource(i, { type: e.target.value as SourceType })
                  }
                >
                  <option value="path">path</option>
                  <option value="url">url</option>
                  <option value="text">text</option>
                </select>
                <button
                  type="button"
                  className="text-xs text-danger px-2"
                  onClick={() => removeSource(i)}
                >
                  ×
                </button>
              </div>
              <textarea
                className="input font-mono text-[11px]"
                rows={s.type === 'text' ? 4 : 1}
                value={s.value ?? ''}
                onChange={(e) => updateSource(i, { value: e.target.value })}
                placeholder={t('panel.knowledge.sourceValuePlaceholder', { type: s.type })}
              />
            </div>
          ))}
          <button
            type="button"
            className="btn-secondary w-full text-xs"
            onClick={addSource}
          >
            + {t('panel.knowledge.addSource')}
          </button>
        </div>
      </div>
    </div>
  )
}