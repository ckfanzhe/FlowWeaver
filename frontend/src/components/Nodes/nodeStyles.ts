/**
 * Visual style per node type — manifest-driven.
 *
 * Color, text, palette-order, i18n-key, and category are now
 * server-driven via `/api/v1/node-types` (the manifest endpoint).
 * Icons remain a frontend concern (React SVG components cannot be
 * JSON-serialized) — `ICON_BY_MANIFEST_NAME` keys icons by the
 * manifest's `icon` string so a manifest-vs-icon drift fails loudly
 * (no silent placeholder).
 *
 * `useNodeVisuals()` is the React hook consumers should call inside
 * components that need the per-type visuals. It blocks on the
 * manifest fetch in `App.tsx` so the hook can return synchronously
 * after first mount. A static fallback (the hardcoded `FALLBACK_*`
 * below) covers the very first paint before the manifest is loaded.
 *
 * `input` and `output` are NOT node types — the workflow's input
 * comes from `Workflow.run(input=...)` and the output is the last
 * Step's result.
 */
import type { NodeType } from '../../types/workflow'
import { useEffect, useState } from 'react'
import {
  fetchNodeTypesManifest,
  type NodeTypeManifestEntry,
  type NodeTypesManifest,
} from '../../api/nodeTypes'
import { ICON_BY_MANIFEST_NAME, UnknownIcon } from './NodeIcons'
import { NODE_FALLBACK_MANIFEST } from './nodeFallback.generated'
import { walkExtends } from '../../lib/manifest'

export interface NodeVisual {
  color: string
  text: string
  Icon: React.FC<{ className?: string }>
  /** i18n key suffix for label/description, e.g. "nodes.agent.label" */
  i18nKey: NodeType
  /** Display name from the manifest (e.g. "Agent"). */
  displayName: string
  /** paletteOrder from the manifest — drives the order shown in the palette. */
  paletteOrder: number
  /** "executable" | "tool_source" — legacy field kept for
   *  consumers that haven't migrated to `kind` yet. */
  category: 'executable' | 'tool_source'
}

// ─────────────────────────────────────────────────────────────────
// Manifest → NodeVisual adapter
// ─────────────────────────────────────────────────────────────────
//
// phase : changed `entryToVisual` to return a
// sentinel UNKNOWN_VISUAL instead of throwing on an unknown icon.
//
// Rationale: the previous behaviour took the whole canvas down if a
// single manifest entry referenced an icon name the frontend hadn't
// registered. Worse, it only ever surfaced during paint of the
// affected node — by then the user had already lost their work
// mid-drag. The new behaviour logs once in dev (Vite's import.meta.env
// is tree-shaken in production) and renders a labelled placeholder so
// the rest of the canvas keeps working. The CI check
// `scripts/check_node_fallback_consistency.py` catches the drift at
// build time, so the runtime fallback is only a safety net.
function entryToVisual(type: NodeType, e: NodeTypeManifestEntry): NodeVisual {
  const Icon = ICON_BY_MANIFEST_NAME[e.icon]
  if (!Icon) {
    if (import.meta.env.DEV) {
      // eslint-disable-next-line no-console
      console.warn(
        `[nodeStyles] unknown icon ${JSON.stringify(e.icon)} for type ` +
          `${JSON.stringify(type)} — rendering placeholder. Add the icon ` +
          'to NodeIcons.ICON_BY_MANIFEST_NAME.',
      )
    }
    return UNKNOWN_VISUAL(type, e)
  }
  return {
    color: e.color,
    text: e.textColor,
    Icon,
    i18nKey: type,
    displayName: e.displayName,
    paletteOrder: e.paletteOrder,
    category: e.category,
  }
}

/**
 * Sentinel visual for manifest entries that reference an icon the
 * frontend hasn't registered. Renders a generic square + the type
 * name so the user can still see WHICH node is broken (instead of a
 * blank rectangle). Production keeps this quiet; dev warns once.
 *
 * The `UnknownIcon` component itself lives in `NodeIcons.tsx` next
 * to the other icon components — `.ts` files can't carry JSX.
 */
function UNKNOWN_VISUAL(type: NodeType, e: NodeTypeManifestEntry): NodeVisual {
  return {
    color: 'border-amber-400/60 bg-amber-50 dark:bg-amber-950',
    text: 'text-amber-700 dark:text-amber-300',
    Icon: UnknownIcon,
    i18nKey: type,
    displayName: e.displayName,
    paletteOrder: e.paletteOrder,
    category: e.category,
  }
}

/** Synchronous manifest accessor — only valid after the manifest has
 * been fetched once. Components should use `useNodeVisuals()` rather
 * than calling this directly so they re-render after the fetch. */
export function manifestToVisuals(m: NodeTypesManifest): {
  visuals: Record<NodeType, NodeVisual>
  order: NodeType[]
} {
  const visuals = {} as Record<NodeType, NodeVisual>
  for (const [type, entry] of Object.entries(m.entries)) {
    visuals[type as NodeType] = entryToVisual(type as NodeType, entry)
  }
  return { visuals, order: m.types }
}

/**
 * Resolve a node type to its `NodeVisual`, walking the `extends`
 * chain. Preset types (wikipedia / brave_search / open_meteo / …)
 * inherit their parent's visual automatically — same lookup rule as
 * `resolveForm()` in PropertyPanel/forms/registry.ts.
 *
 * The walk is bounded by `maxDepth` so a cycle in the manifest
 * (which the backend already rejects at load time, but the frontend
 * shouldn't depend on) can't infinite-loop.
 *
 * @returns the visual, or `null` if neither the type nor any of its
 *          parents has one registered. Callers must handle `null` —
 *          a missing visual usually means a stale workflow that
 *          references a node type which has since been removed from
 *          the manifest.
 */
export function resolveVisual(
  type: NodeType,
  visuals: Record<NodeType, NodeVisual>,
  manifest: NodeTypesManifest | null,
  maxDepth = 8,
): NodeVisual | null {
  // 1. Direct hit — most common path.
  const direct = visuals[type]
  if (direct) return direct

  // 2. Walk `extends` chain. phase : moved the loop
  //    body to `lib/manifest.walkExtends` so the same code path
  //    resolves both visuals and forms.
  if (!manifest) return null
  return walkExtends(
    type,
    manifest,
    (name) => visuals[name as NodeType] ?? null,
    maxDepth,
  )
}

// ─────────────────────────────────────────────────────────────────
// First-paint fallback (phase, )
// ─────────────────────────────────────────────────────────────────
// Used for the literal first paint, before the manifest fetch
// resolves. The fallback is generated from `shared/nodes.manifest.json`
// by `scripts/generate_node_fallback.py` — no hand-maintained copy,
// no drift. The companion CI check
// `scripts/check_node_fallback_consistency.py` fails the build if the
// generated file is stale relative to the manifest.
const FALLBACK = manifestToVisuals(NODE_FALLBACK_MANIFEST)

/**
 * Manifest-driven visuals. The first paint uses a hardcoded fallback
 * (so SSR / pre-fetch paint isn't blank); the first `useEffect`
 * replaces it with the live manifest values from the backend.
 *
 * Consumers that previously read `NODE_VISUALS` / `V1_NODE_TYPES`
 * from this module should switch to the hook-returned `visuals` and
 * `order`. The legacy top-level exports are kept as the fallback
 * values for components that haven't migrated yet.
 */
export function useNodeVisuals(): {
  visuals: Record<NodeType, NodeVisual>
  order: NodeType[]
  manifest: NodeTypesManifest | null
} {
  const [manifest, setManifest] = useState<NodeTypesManifest | null>(null)
  useEffect(() => {
    let cancelled = false
    fetchNodeTypesManifest()
      .then((m) => {
        if (!cancelled) setManifest(m)
      })
      .catch((err) => {
        // Keep the fallback on error — palette still renders something.
        // The console warning makes the issue obvious during dev.
        console.warn('failed to fetch node-types manifest, using fallback', err)
      })
    return () => {
      cancelled = true
    }
  }, [])
  if (!manifest) return { ...FALLBACK, manifest: null }
  return { ...manifestToVisuals(manifest), manifest }
}

// phase : the previous legacy top-level exports
// (`NODE_VISUALS` / `V1_NODE_TYPES`) were deleted — they were the
// last callers of the hand-maintained `FALLBACK_MANIFEST` block and
// had no actual consumers once the phase manifest-driven migration
// landed. Components now call `useNodeVisuals()` (the React hook
// above) or read `manifestToVisuals()` directly.