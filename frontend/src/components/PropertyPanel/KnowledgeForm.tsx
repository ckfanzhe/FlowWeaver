/**
 * Form for the `knowledge` node — RAG / vector DB source.
 *
 * Parallel to `ToolForm` (mode-aware dispatcher). Three section layout:
 *   1. Identity + behaviour (`name`, `maxResults`, `addKnowledgeToContext`).
 *   2. Vector DB picker (lancedb | pgvector | chroma) + per-backend
 *      fields below.
 *   3. Embedder picker (openai | sentence_transformers | cohere) +
 *      per-embedder fields below.
 *   4. Sources list (`+ Add` row → path/url/text with optional reader).
 *
 * The vector DB / embedder discriminators live in local state so the
 * conditional sub-form render is synchronous with the user's picker
 * change (the underlying `useWorkflowStore` write is async). Same
 * `useEffect`-sync pattern as `useSource` in ToolForm (line 48).
 *
 * No preset-discriminator collapse for knowledge — the schema keeps
 * a flat `vectorDb` + `embedder` discriminator pair; no per-backend
 * preset rows in the manifest. Adding a backend = adding a Literal
 * member + a row here, not a new node type.
 */
import { useState, useEffect } from 'react'
import { useWorkflowStore } from '../../store/workflowStore'
import { useT } from '../../i18n'
import type { KnowledgeNodeConfig, KnowledgeSource } from '../../types/workflow'
import { Field, NodeDataField } from './primitives'

type VectorDb = KnowledgeNodeConfig['vectorDb']
type Embedder = KnowledgeNodeConfig['embedder']
type SourceType = KnowledgeSource['type']

function useLocalMirror<T>(storeValue: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(storeValue)
  useEffect(() => {
    setV(storeValue)
  }, [storeValue])
  return [v, setV]
}

export function KnowledgeForm({ nodeId }: { nodeId: string }) {
  const t = useT()
  // `vectorDb` / `embedder` discriminators live in local state so the
  // conditional sub-form renders synchronously with picker changes.
  const storeVectorDb = useWorkflowStore((s) => {
    const n = s.nodes.find((nn) => nn.id === nodeId)
    return (n?.data?.config as KnowledgeNodeConfig | undefined)?.vectorDb ?? 'lancedb'
  })
  const storeEmbedder = useWorkflowStore((s) => {
    const n = s.nodes.find((nn) => nn.id === nodeId)
    return (n?.data?.config as KnowledgeNodeConfig | undefined)?.embedder ?? 'openai'
  })
  const [vectorDb, setVectorDb] = useLocalMirror<VectorDb>(storeVectorDb)
  const [embedder, setEmbedder] = useLocalMirror<Embedder>(storeEmbedder)

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

      {/* ── Vector DB ─────────────────────────────────────────────── */}
      <Field label={t('panel.knowledge.vectorDb')}>
        <select
          className="input"
          value={vectorDb}
          onChange={(e) => {
            const v = e.target.value as VectorDb
            setVectorDb(v)
            // Mirror to the store so `node.data.config.vectorDb` is
            // consistent. The local-state copy above mirrors it back
            // on next render (useEffect).
            const cfg = ((useWorkflowStore.getState().nodes.find((n) => n.id === nodeId)?.data?.config ?? {}) as Record<string, unknown>)
            const merged = structuredClone(cfg)
            merged.vectorDb = v
            update(nodeId, { config: merged })
          }}
        >
          <option value="lancedb">lancedb (default)</option>
          <option value="pgvector">pgvector</option>
          <option value="chroma">chroma</option>
        </select>
      </Field>

      {vectorDb === 'lancedb' && (
        <>
          <NodeDataField<string> nodeId={nodeId} path={['lancedbUri']}>
            {(value, set) => (
              <Field label={t('panel.knowledge.lancedbUri')}>
                <input
                  className="input font-mono text-[11px]"
                  value={value ?? '/tmp/lancedb'}
                  onChange={(e) => set(e.target.value)}
                />
              </Field>
            )}
          </NodeDataField>
          <NodeDataField<string> nodeId={nodeId} path={['lancedbTableName']}>
            {(value, set) => (
              <Field label={t('panel.knowledge.lancedbTableName')}>
                <input
                  className="input"
                  value={value ?? 'agno_kb'}
                  onChange={(e) => set(e.target.value)}
                />
              </Field>
            )}
          </NodeDataField>
        </>
      )}

      {vectorDb === 'pgvector' && (
        <>
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
        </>
      )}

      {vectorDb === 'chroma' && (
        <>
          <NodeDataField<string> nodeId={nodeId} path={['chromaPath']}>
            {(value, set) => (
              <Field label={t('panel.knowledge.chromaPath')}>
                <input
                  className="input font-mono text-[11px]"
                  value={value ?? './chroma_db'}
                  onChange={(e) => set(e.target.value)}
                />
              </Field>
            )}
          </NodeDataField>
          <NodeDataField<string> nodeId={nodeId} path={['chromaCollectionName']}>
            {(value, set) => (
              <Field label={t('panel.knowledge.chromaCollectionName')}>
                <input
                  className="input"
                  value={value ?? 'agno_kb'}
                  onChange={(e) => set(e.target.value)}
                />
              </Field>
            )}
          </NodeDataField>
          <NodeDataField<boolean> nodeId={nodeId} path={['chromaPersistentClient']}>
            {(value, set) => (
              <label className="flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={value !== false}
                  onChange={(e) => set(e.target.checked)}
                />
                {t('panel.knowledge.chromaPersistentClient')}
              </label>
            )}
          </NodeDataField>
        </>
      )}

      {/* ── Embedder ──────────────────────────────────────────────── */}
      <Field label={t('panel.knowledge.embedder')}>
        <select
          className="input"
          value={embedder}
          onChange={(e) => {
            const v = e.target.value as Embedder
            setEmbedder(v)
            const cfg = ((useWorkflowStore.getState().nodes.find((n) => n.id === nodeId)?.data?.config ?? {}) as Record<string, unknown>)
            const merged = structuredClone(cfg)
            merged.embedder = v
            update(nodeId, { config: merged })
          }}
        >
          <option value="openai">openai (default)</option>
          <option value="sentence_transformers">sentence_transformers</option>
          <option value="cohere">cohere</option>
        </select>
      </Field>

      {embedder === 'openai' && (
        <>
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
        </>
      )}

      {embedder === 'sentence_transformers' && (
        <>
          <NodeDataField<string> nodeId={nodeId} path={['sentenceTransformersModel']}>
            {(value, set) => (
              <Field label={t('panel.knowledge.sentenceTransformersModel')}>
                <input
                  className="input font-mono text-[11px]"
                  value={value ?? 'sentence-transformers/all-MiniLM-L6-v2'}
                  onChange={(e) => set(e.target.value)}
                />
              </Field>
            )}
          </NodeDataField>
          <NodeDataField<number> nodeId={nodeId} path={['sentenceTransformersDimensions']}>
            {(value, set) => (
              <Field label={t('panel.knowledge.sentenceTransformersDimensions')}>
                <input
                  className="input"
                  type="number"
                  min={1}
                  max={4096}
                  value={value ?? 384}
                  onChange={(e) => set(parseInt(e.target.value || '384', 10))}
                />
              </Field>
            )}
          </NodeDataField>
        </>
      )}

      {embedder === 'cohere' && (
        <>
          <NodeDataField<string> nodeId={nodeId} path={['cohereModel']}>
            {(value, set) => (
              <Field label={t('panel.knowledge.cohereModel')}>
                <input
                  className="input"
                  value={value ?? 'embed-english-v3.0'}
                  onChange={(e) => set(e.target.value)}
                />
              </Field>
            )}
          </NodeDataField>
          <NodeDataField<string> nodeId={nodeId} path={['cohereApiKey']}>
            {(value, set) => (
              <Field label={t('panel.knowledge.cohereApiKey')}>
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
          <NodeDataField<string> nodeId={nodeId} path={['cohereInputType']}>
            {(value, set) => (
              <Field label={t('panel.knowledge.cohereInputType')}>
                <select
                  className="input"
                  value={value ?? 'search_query'}
                  onChange={(e) => set(e.target.value)}
                >
                  <option value="search_query">search_query</option>
                  <option value="search_document">search_document</option>
                </select>
              </Field>
            )}
          </NodeDataField>
        </>
      )}

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
