/**
 * Form for `tool` + `preset='<name>'` — currently wikipedia /
 * tavily_search / duckduckgo / calculator / arxiv_search (,
 * ).
 *
 * collapse: this form is no longer a top-level form
 * mapped to a node type — it now renders INSIDE `ToolForm` when
 * `cfg.preset` is set, exposing the toolkit-specific knobs:
 *
 *   - `enabled_methods`: which toolkit methods to expose. Empty /
 *     missing → expose ALL methods declared in `PRESET_REGISTRY`.
 *     Non-empty → intersect (unknown names surface as warnings at
 *     runtime — see `tool_factories.build_toolkit_for_preset`).
 *   - `toolkit_options`: free-form key/value pairs passed as
 *     **kwargs to the toolkit constructor (api_key, enable_search,
 *     search_depth, ...).
 *
 * The wikipedia preset is HTTP-backed (no toolkit), so its method
 * picker renders no checkboxes — the `enabled_methods` /
 * `toolkit_options` fields only matter for the 4 toolkit presets.
 *
 * row D : the field names match the Python attrs
 *     in `ToolNodeConfig` — `enabled_methods` / `toolkit_options`
 *     (snake_case). They were previously written as camelCase
 *     (`enabledMethods` / `toolkitOptions`) which the backend
 *     silently ignored under `extra="ignore"`, so every preset's
 *     settings were dropped on save. Switching to snake_case
 *     aligns the frontend with the Pydantic mirror and unblocks
 *     the values at runtime. : the prior
 *     `ToolsNodeConfig` collapsed into the merged `ToolNodeConfig` —
 *     `enabled_methods` / `toolkit_options` still live there.
 *     : `PRESET_REGISTRY` becomes the source
 *     of truth for the per-preset toolkit method list (replacing the
 *     prior `runtime.toolkitMethods` on the manifest's per-preset
 *     entry). The frontend keeps a parallel mirror so the form can
 *     render the method picker without an extra API round-trip;
 *     server-side validation rejects any drift.
 */
import { useT } from '../../i18n'
import { useWorkflowStore } from '../../store/workflowStore'
import type { ToolNodeConfig } from '../../types/workflow'
import { Field, JsonField, NodeDataField } from './primitives'

/**
 * : mirror of `app.core.strategies.tool.PRESET_REGISTRY`.
 * Only the toolkit presets carry `toolkit_methods` — wikipedia is
 * HTTP-backed and the `enabled_methods` / `toolkit_options` fields
 * are no-ops for it. The frontend uses this to render the method
 * picker; the server uses `PRESET_REGISTRY` as the source of truth
 * (it accepts the rendered output and intersects against its own
 * allowed list).
 */
const PRESET_TOOLKIT_METHODS: Partial<
  Record<NonNullable<ToolNodeConfig['preset']>, readonly string[]>
> = {
  tavily_search: ['web_search_using_tavily'],
  duckduckgo: ['web_search'],
  calculator: ['add', 'subtract', 'multiply', 'divide'],
  arxiv_search: ['search_arxiv_and_return_articles'],
}

export function ToolPresetForm({ nodeId }: { nodeId: string }) {
  const t = useT()
  const preset = useWorkflowStore((s) => {
    const n = s.nodes.find((nn) => nn.id === nodeId)
    return (n?.data?.config as ToolNodeConfig | undefined)?.preset ?? null
  })
  const declared = preset ? (PRESET_TOOLKIT_METHODS[preset] ?? []) : []
  return (
    <div className="space-y-3">
      <div className="rounded border border-edge bg-surface-2 px-2 py-1.5 text-[11px] text-ink-muted">
        {t('panel.toolPreset.bundled')}
      </div>
      {declared.length > 0 ? (
        <EnabledMethodsSection nodeId={nodeId} declared={declared} />
      ) : (
        <p className="text-[11px] text-ink-muted">
          {t('panel.toolPreset.noDeclaredMethods')}
        </p>
      )}
      <NodeDataField<Record<string, unknown>> nodeId={nodeId} path={['toolkit_options']}>
        {(v, set) => (
          <Field label={t('panel.toolPreset.toolkitOptions')}>
            <JsonField
              value={v ?? {}}
              onChange={set}
              placeholder='{"api_key": "..."}'
              rows={4}
            />
          </Field>
        )}
      </NodeDataField>
    </div>
  )
}

/**
 * Renders a checkbox per `PRESET_TOOLKIT_METHODS[preset]`-declared
 * method. Empty selection (all unchecked) is the "expose everything"
 * sentinel — the backend treats `enabled_methods == []` as the
 * default. We translate that to the UI by leaving all checkboxes
 * unchecked and showing a hint; checking any subset writes the
 * explicit list.
 */
function EnabledMethodsSection({
  nodeId,
  declared,
}: {
  nodeId: string
  declared: readonly string[]
}) {
  const t = useT()
  return (
    <Field label={t('panel.toolPreset.enabledMethods')}>
      <NodeDataField<string[]> nodeId={nodeId} path={['enabled_methods']}>
        {(enabled, setEnabled) => {
          const list = enabled ?? []
          const allOff = list.length === 0
          const toggle = (m: string, on: boolean) => {
            const next = new Set(list)
            if (on) next.add(m)
            else next.delete(m)
            setEnabled(Array.from(next).sort())
          }
          return (
            <div className="space-y-1">
              <p className="text-[11px] text-ink-muted">
                {allOff
                  ? t('panel.toolPreset.allExposedHint')
                  : t('panel.toolPreset.subsetHint', { count: list.length })}
              </p>
              {declared.map((m) => (
                <label key={m} className="flex items-center gap-2 text-[12px]">
                  <input
                    type="checkbox"
                    className="accent-accent"
                    checked={allOff || list.includes(m)}
                    // "all off" sentinel: clicking the first checkbox
                    // turns on ONLY that method; subsequent toggles
                    // mutate the list normally.
                    onChange={(e) => {
                      if (allOff) {
                        setEnabled([m])
                      } else {
                        toggle(m, e.target.checked)
                      }
                    }}
                  />
                  <code className="font-mono text-[11px]">{m}</code>
                </label>
              ))}
            </div>
          )
        }}
      </NodeDataField>
    </Field>
  )
}