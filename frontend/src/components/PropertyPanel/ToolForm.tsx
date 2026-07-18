/**
 * Form for the unified `tool` node — mode-aware dispatcher.
 *
 * : replaces the prior standalone
 * `HttpForm` + `McpForm` + `ToolsForm`. The 3 forms collapse to one
 * `ToolForm` whose `cfg.source` discriminator (`'http'` | `'mcp'` |
 * `'function'`) picks which sub-form to render:
 *
 *   - `source='http'`     → HTTP wrapper form (was HttpForm)
 *   - `source='mcp'`      → MCP server picker (was McpForm)
 *   - `source='function'` → User-functions list editor (was ToolsForm)
 *
 * The 3 sub-form bodies are carried over verbatim from the deleted
 * files (and continue to share the same `useT()` i18n keys — see
 * `panel.http.*` / `panel.mcp.*` / `panel.tools.*`). Only the
 * discriminator wrapper is new.
 *
 * The `source` selector is at the top; switching source keeps the
 * existing per-source config and just swaps which fields are
 * visible. A freshly-dropped `tool` node defaults to
 * `source='function'` per the manifest.
 *
 * : the 5 preset tool types collapsed into the
 * `tool` node's `preset` config discriminator. When `cfg.preset` is
 * set, the form renders a preset selector + the toolkit picker
 * (`ToolPresetForm`) and the source selector is hidden (presets
 * force their own source — wikipedia → http, the 4 toolkits
 * bypass `source` entirely and route through
 * `build_toolkit_for_preset`).
 */
import { useState, useEffect } from 'react'
import { useSettingsStore } from '../../store/settingsStore'
import { useWorkflowStore } from '../../store/workflowStore'
import { useT } from '../../i18n'
import type { ToolNodeConfig } from '../../types/workflow'
import { Field, JsonField, NodeDataField } from './primitives'
import { ToolPresetForm } from './ToolPresetForm'

/**
 * : the source discriminator lives in
 * `cfg.source`. We mirror its current value into local state so the
 * conditional sub-form render is synchronous with the user's
 * selector change (the underlying `useWorkflowStore` write is async;
 * the local copy is the source-of-truth for the conditional render
 * below until the next `useEffect` tick syncs from any external
 * change).
 */
function useSource(nodeId: string): [ToolNodeConfig['source'], (s: ToolNodeConfig['source']) => void] {
  const cfg = useWorkflowStore((s) => {
    const n = s.nodes.find((nn) => nn.id === nodeId)
    return (n?.data?.config as ToolNodeConfig | undefined)?.source ?? 'function'
  })
  const [source, setSource] = useState<ToolNodeConfig['source']>(cfg)
  // Re-sync when the store value diverges (e.g. another panel write).
  useEffect(() => {
    if (cfg !== source) setSource(cfg)
  }, [cfg, source])
  return [source, setSource]
}

export function ToolForm({ nodeId }: { nodeId: string }) {
  const t = useT()
  const [source, setSource] = useSource(nodeId)
  // : preset discriminator — when set, the
  // preset forces its own source and we render the toolkit
  // sub-form (`ToolPresetForm`) instead of the source picker.
  const preset = useWorkflowStore((s) => {
    const n = s.nodes.find((nn) => nn.id === nodeId)
    return (n?.data?.config as ToolNodeConfig | undefined)?.preset ?? null
  })
  const [activePreset, setActivePreset] = useState<
    ToolNodeConfig['preset'] | null
  >(preset)
  useEffect(() => {
    if (preset !== activePreset) setActivePreset(preset)
  }, [preset, activePreset])

  return (
    <>
      {/* : preset discriminator — null = plain
          `tool` node; one of the 5 preset names = toolkit preset
          (tavily_search / duckduckgo / calculator / arxiv_search)
          or HTTP preset (wikipedia). When set, `source` is forced
          by PRESET_REGISTRY (toolkit presets bypass source entirely;
          wikipedia forces source='http'). */}
      <NodeDataField<ToolNodeConfig['preset']> nodeId={nodeId} path={['preset']}>
        {(v, set) => (
          <Field label={t('panel.fields.preset')}>
            <select
              className="input"
              value={v ?? ''}
              onChange={(e) => {
                const next = e.target.value as ToolNodeConfig['preset']
                set(next || null)
                setActivePreset(next || null)
              }}
            >
              <option value="">{t('panel.tool.presetNone')}</option>
              <option value="wikipedia">Wikipedia</option>
              <option value="tavily_search">Tavily Search</option>
              <option value="duckduckgo">DuckDuckGo Search</option>
              <option value="calculator">Calculator</option>
              <option value="arxiv_search">arXiv Search</option>
            </select>
            <p className="mt-1 text-[10px] text-ink-muted leading-snug">
              {t('panel.tool.presetHint')}
            </p>
          </Field>
        )}
      </NodeDataField>

      {/* When a preset is active, render the toolkit picker
          (`ToolPresetForm`) which exposes `enabled_methods` +
          `toolkit_options`. The source picker is hidden — presets
          force their own source. The wikipedia preset additionally
          shows the HTTP fields below (it forces source='http'). */}
      {activePreset && <ToolPresetForm nodeId={nodeId} />}

      {/* : source discriminator — hidden when a preset is
          active (preset forces its own source). */}
      {!activePreset && (
        <NodeDataField<ToolNodeConfig['source']> nodeId={nodeId} path={['source']}>
          {(v, set) => (
            <Field label={t('panel.fields.source')}>
              <select
                className="input"
                value={v ?? 'function'}
                onChange={(e) => {
                  const next = e.target.value as ToolNodeConfig['source']
                  set(next)
                  setSource(next)
                }}
              >
                <option value="http">{t('panel.tool.sourceHttp')}</option>
                <option value="mcp">{t('panel.tool.sourceMcp')}</option>
                <option value="function">{t('panel.tool.sourceFunction')}</option>
              </select>
              <p className="mt-1 text-[10px] text-ink-muted leading-snug">
                {t('panel.tool.sourceHint')}
              </p>
            </Field>
          )}
        </NodeDataField>
      )}

      {/* Carried over from HttpForm (verbatim body). Rendered for
          wikipedia preset (which forces source='http') or any plain
          `tool` node the user picked http for. */}
      {(activePreset === 'wikipedia' || (!activePreset && source === 'http')) && (
        <HttpSubForm nodeId={nodeId} />
      )}

      {/* Carried over from McpForm (verbatim body). Hidden when a
          preset is active — toolkit presets bypass MCP; wikipedia
          forces http. */}
      {!activePreset && source === 'mcp' && <McpSubForm nodeId={nodeId} />}

      {/* Carried over from ToolsForm (verbatim body). Hidden when a
          preset is active — toolkit presets route through
          `build_toolkit_for_preset`; wikipedia forces http. */}
      {!activePreset && source === 'function' && <FunctionsSubForm nodeId={nodeId} />}
    </>
  )
}

/* ─────────────────────────────────────────────────────────────────
 * Sub-forms — bodies verbatim from the deleted HttpForm/McpForm/ToolsForm.
 * Kept in the same file so the source switcher can co-locate them
 * without dragging in 3 separate imports.
 * ───────────────────────────────────────────────────────────────── */

function HttpSubForm({ nodeId }: { nodeId: string }) {
  const t = useT()
  return (
    <>
      <NodeDataField<string> nodeId={nodeId} path={['toolName']}>
        {(v, set) => (
          <Field label={t('panel.fields.toolName')}>
            <input
              type="text"
              className="input"
              value={v ?? ''}
              onChange={(e) => set(e.target.value)}
              placeholder={t('panel.http.toolNamePlaceholder')}
            />
          </Field>
        )}
      </NodeDataField>
      <NodeDataField<string> nodeId={nodeId} path={['toolDescription']}>
        {(v, set) => (
          <Field label={t('panel.fields.toolDescription')}>
            <input
              type="text"
              className="input"
              value={v ?? ''}
              onChange={(e) => set(e.target.value)}
              placeholder={t('panel.http.toolDescriptionPlaceholder')}
            />
          </Field>
        )}
      </NodeDataField>
      <NodeDataField<ToolNodeConfig['method']> nodeId={nodeId} path={['method']}>
        {(method, setMethod) => (
          <Field label={t('panel.fields.method')}>
            <select
              className="input"
              value={method ?? 'GET'}
              onChange={(e) => setMethod(e.target.value as ToolNodeConfig['method'])}
            >
              <option value="GET">GET</option>
              <option value="POST">POST</option>
            </select>
          </Field>
        )}
      </NodeDataField>
      <NodeDataField<string> nodeId={nodeId} path={['baseUrl']}>
        {(v, set) => (
          <Field label={t('panel.fields.baseUrl')}>
            <input
              type="text"
              className="input"
              value={v ?? ''}
              onChange={(e) => set(e.target.value)}
              placeholder={t('panel.http.baseUrlPlaceholder')}
            />
          </Field>
        )}
      </NodeDataField>
      <NodeDataField<string> nodeId={nodeId} path={['path']}>
        {(v, set) => (
          <Field label={t('panel.fields.path')}>
            <input
              type="text"
              className="input"
              value={v ?? ''}
              onChange={(e) => set(e.target.value)}
              placeholder={t('panel.http.pathPlaceholder')}
            />
          </Field>
        )}
      </NodeDataField>
      <NodeDataField<Record<string, string>> nodeId={nodeId} path={['headers']}>
        {(v, set) => (
          <Field label={t('panel.fields.headers')}>
            <JsonField
              value={v}
              onChange={(x) => set(x as Record<string, string>)}
              placeholder={t('panel.http.headersPlaceholder')}
            />
          </Field>
        )}
      </NodeDataField>
      <NodeDataField<Record<string, string>> nodeId={nodeId} path={['queryParams']}>
        {(v, set) => (
          <Field label={t('panel.fields.queryParams')}>
            <JsonField
              value={v}
              onChange={(x) => set(x as Record<string, string>)}
              placeholder={t('panel.http.queryParamsPlaceholder')}
            />
          </Field>
        )}
      </NodeDataField>
      <NodeDataField<string> nodeId={nodeId} path={['authToken']}>
        {(v, set) => (
          <Field label={t('panel.fields.authToken')}>
            <input
              type="password"
              className="input"
              value={v ?? ''}
              onChange={(e) => set(e.target.value)}
              placeholder={t('panel.http.authTokenPlaceholder')}
            />
          </Field>
        )}
      </NodeDataField>
      <NodeDataField<string> nodeId={nodeId} path={['bodySchema']}>
        {(v, set) => (
          <Field label={t('panel.fields.bodySchema')}>
            <JsonField
              value={v}
              onChange={(x) => set(x as string)}
              placeholder={t('panel.http.bodySchemaPlaceholder')}
              rows={4}
            />
          </Field>
        )}
      </NodeDataField>
    </>
  )
}

function McpSubForm({ nodeId }: { nodeId: string }) {
  const t = useT()
  const servers = useSettingsStore((s) => s.mcpServers)
  return (
    <>
      <NodeDataField<ToolNodeConfig['serverId']> nodeId={nodeId} path={['serverId']}>
        {(serverId, setServerId) => (
          <Field label={t('panel.fields.serverId')}>
            <select
              className="input"
              value={serverId ?? ''}
              onChange={(e) => setServerId(e.target.value)}
            >
              <option value="">{t('panel.mcp.selectServer')}</option>
              {servers.map((s) => (
                <option key={s.id} value={s.id} disabled={!s.enabled}>
                  {s.name} ({s.transport}){s.enabled ? '' : ' · disabled'}
                </option>
              ))}
            </select>
            {servers.length === 0 && (
              <p className="mt-1 text-[10px] text-ink-muted">
                {t('panel.mcp.noServers')}
              </p>
            )}
          </Field>
        )}
      </NodeDataField>
      <NodeDataField<string> nodeId={nodeId} path={['toolNamePrefix']}>
        {(prefix, setPrefix) => (
          <Field label={t('panel.fields.toolNamePrefix')}>
            <input
              type="text"
              className="input"
              value={prefix ?? ''}
              onChange={(e) => setPrefix(e.target.value)}
              placeholder=""
            />
            <p className="mt-1 text-[10px] text-ink-muted leading-snug">
              {t('panel.mcp.prefixHint')}
            </p>
          </Field>
        )}
      </NodeDataField>
    </>
  )
}

import type { ParamSchema, ToolFunction } from '../../types/workflow'
function FunctionsSubForm({ nodeId }: { nodeId: string }) {
  const t = useT()
  return (
    <NodeDataField<ToolFunction[]> nodeId={nodeId} path={['functions']}>
      {(fns, setFns) => {
        const list = fns ?? []
        const addFn = () => {
          setFns([
            ...list,
            { name: 'my_tool', description: '', parameters: [], code: 'def my_tool():\n    """TODO"""\n    return {}\n' },
          ])
        }
        const updateFn = (idx: number, patch: Partial<ToolFunction>) => {
          setFns(list.map((f, i) => (i === idx ? { ...f, ...patch } : f)))
        }
        const removeFn = (idx: number) => {
          setFns(list.filter((_, i) => i !== idx))
        }
        return (
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <div className="field-label !mb-0">{t('panel.fields.functions')}</div>
              <button
                type="button"
                className="text-xs text-accent-text hover:underline"
                onClick={addFn}
              >
                {t('panel.fields.addFunction')}
              </button>
            </div>
            {list.length === 0 && (
              <p className="text-[11px] text-ink-muted">{t('panel.tools.noFunctions')}</p>
            )}
            {list.map((f, idx) => (
              <div
                key={idx}
                className="rounded border border-edge bg-surface-2 p-2 space-y-2"
              >
                <div className="flex items-center gap-2">
                  <input
                    type="text"
                    className="input flex-1"
                    value={f.name}
                    onChange={(e) => updateFn(idx, { name: e.target.value })}
                    placeholder={t('panel.fields.functionName')}
                  />
                  <button
                    type="button"
                    className="text-xs text-danger hover:underline"
                    onClick={() => removeFn(idx)}
                  >
                    ✕
                  </button>
                </div>
                <input
                  type="text"
                  className="input"
                  value={f.description}
                  onChange={(e) => updateFn(idx, { description: e.target.value })}
                  placeholder={t('panel.fields.functionDescription')}
                />
                <textarea
                  className="input font-mono text-[11px]"
                  rows={6}
                  value={f.code}
                  onChange={(e) => updateFn(idx, { code: e.target.value })}
                  placeholder={t('panel.fields.functionCode')}
                />
                <details className="text-[11px]">
                  <summary className="cursor-pointer text-ink-muted">
                    {t('panel.fields.functionParams')} ({f.parameters.length})
                  </summary>
                  <div className="mt-2 space-y-1">
                    {f.parameters.map((p, pi) => (
                      <div key={pi} className="flex items-center gap-1">
                        <input
                          type="text"
                          className="input flex-1"
                          value={p.name}
                          onChange={(e) =>
                            updateFn(idx, {
                              parameters: f.parameters.map((pp, ppi) =>
                                ppi === pi ? { ...pp, name: e.target.value } : pp
                              ),
                            })
                          }
                        />
                        <select
                          className="input"
                          value={p.type}
                          onChange={(e) =>
                            updateFn(idx, {
                              parameters: f.parameters.map((pp, ppi) =>
                                ppi === pi ? { ...pp, type: e.target.value as ParamSchema['type'] } : pp
                              ),
                            })
                          }
                        >
                          <option value="string">string</option>
                          <option value="number">number</option>
                          <option value="boolean">boolean</option>
                          <option value="object">object</option>
                        </select>
                        <button
                          type="button"
                          className="text-xs text-danger"
                          onClick={() =>
                            updateFn(idx, {
                              parameters: f.parameters.filter((_, ppi) => ppi !== ppi),
                            })
                          }
                        >
                          ✕
                        </button>
                      </div>
                    ))}
                    <button
                      type="button"
                      className="text-[11px] text-accent-text hover:underline"
                      onClick={() =>
                        updateFn(idx, {
                          parameters: [
                            ...f.parameters,
                            { name: 'arg', type: 'string', required: true, description: '' },
                          ],
                        })
                      }
                    >
                      + param
                    </button>
                  </div>
                </details>
              </div>
            ))}
          </div>
        )
      }}
    </NodeDataField>
  )
}