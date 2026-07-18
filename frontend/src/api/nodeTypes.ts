/**
 * Manifest-driven node-type metadata.
 *
 * The canvas / palette / context-menu / template-card all need to know
 * each node type's color, icon-name, palette-order, i18n key, etc. The
 * source of truth lives in `shared/nodes.manifest.json` on the
 * backend; the backend's `/api/v1/node-types` endpoint exposes the
 * metadata as JSON; this client fetches it once and caches it.
 *
 * Icons themselves are React components (SVG bodies) and cannot be
 * JSON-serialized — the `icon` field is the manifest's icon-NAME
 * string. The frontend keeps an `IconByManifestName` map (mirrors
 * the manifest's icon strings) so a missing icon fails loudly instead
 * of rendering a placeholder.
 */
import { useEffect, useState } from 'react'
import type { NodeType } from '../types/workflow'
import { api } from './client'

/**
 * phase : the kind/capabilities fields are how the
 * frontend decides which form / palette group / drop-handler a node
 * type gets. They mirror the backend's `kind` / `capabilities`
 * blocks on each manifest entry.
 */
export type NodeKind = 'executable' | 'compound' | 'tool_source' | 'control_flow'

export interface NodeTypeCapabilities {
  /** Compound-pass ordering (parallel=10, condition=20, loop=30, router=40).
   *  `null` for non-compound types. */
  compoundPass: number | null
  /** True for tool-source types (tool with source discriminator / preset-inherits-tool). */
  isToolSource: boolean
  /** True for agent — accepts tool-attachment edges. */
  needsToolWiring: boolean
  /** Reserved for future compound-skip logic. */
  skipPass1: boolean
  /** "agent" | "ask" | "none" — wraps the runtime Step. 
   *  : `human_input` renamed to `ask`. */
  stepWrapper: 'agent' | 'ask' | 'none'
}

export interface NodeTypeUi {
  /** Palette-group label (e.g. "Core", "Data", "Search"). */
  group: string
  /** Form-component name (e.g. "AgentForm", "ToolForm") — used by
   *  PropertyPanel to dispatch to the right form. */
  form: string
  /** Palette order (lower = earlier). */
  paletteOrder: number
}

export interface NodeTypeManifestEntry {
  /** Legacy: 'executable' or 'tool_source'. Mirrors `kind` but kept
   *  for older consumers — new code should prefer `kind`. */
  category: 'executable' | 'tool_source'
  /** Manifest kind — the runtime category this node belongs to. */
  kind: NodeKind
  /** Parent name for preset inheritance (e.g. "http" for wikipedia). */
  extends: string | null
  displayName: string
  i18nKey: string
  color: string
  textColor: string
  icon: string
  paletteOrder: number
  ui: NodeTypeUi
  capabilities: NodeTypeCapabilities
  /** Resolved default config (preset inheritance applied). */
  defaultConfig: Record<string, unknown>
  /** : the prior `toolkitMethods` field was
   *  removed from the per-type API response — per-preset toolkit
   *  method lists now live in `PRESET_REGISTRY` and are surfaced
   *  via `ToolNodeConfig.enabled_methods` (preset discriminator on
   *  the unified `tool` node). The field stays here as `string[]`
   *  (never present in the response) so older callers that read
   *  `entry.toolkitMethods` get an empty array instead of `undefined`. */
  toolkitMethods: string[]
  io: { inputs: string[]; outputs: string[]; tools: string[] }
}

export interface NodeTypesManifest {
  /** Backend's schema version. v2 added kind/extends/ui/capabilities. */
  schemaVersion: number
  types: NodeType[]
  entries: Record<string, NodeTypeManifestEntry>
}

let _cache: NodeTypesManifest | null = null

/** Fetch (and cache) the manifest once per process. */
export async function fetchNodeTypesManifest(): Promise<NodeTypesManifest> {
  if (_cache) return _cache
  _cache = await api.get<NodeTypesManifest>('/api/v1/node-types')
  return _cache
}

/** Synchronous accessor — only valid after `fetchNodeTypesManifest()`
 * has resolved at least once (the App-level effect does this on mount). */
export function nodeTypesManifest(): NodeTypesManifest {
  if (!_cache) {
    throw new Error(
      'nodeTypesManifest() called before fetch — App should have called ' +
        'fetchNodeTypesManifest() on mount'
    )
  }
  return _cache
}

/**
 * React hook exposing the current manifest. Returns `null` on the
 * first paint (before the fetch resolves); components that need to
 * render synchronously should use `useNodeVisuals()` instead, which
 * has a hardcoded fallback for the first frame.
 */
export function useManifest(): NodeTypesManifest | null {
  const [manifest, setManifest] = useState<NodeTypesManifest | null>(
    _cache,
  )
  useEffect(() => {
    if (_cache) {
      setManifest(_cache)
      return
    }
    let cancelled = false
    fetchNodeTypesManifest()
      .then((m) => {
        if (!cancelled) setManifest(m)
      })
      .catch((err) => {
        console.warn('failed to fetch node-types manifest', err)
      })
    return () => {
      cancelled = true
    }
  }, [])
  return manifest
}

/** Test-only: reset the in-memory cache (used by manifest equivalence
 * tests so a manifest change in the middle of a run re-fetches). */
export function _resetNodeTypesManifestCache(): void {
  _cache = null
}

/**
 * Runtime check: does this string name a node type the platform
 * currently knows about?
 *
 * The compile-time `NodeType` union (generated from the manifest)
 * catches drift at typecheck time, but at runtime the canvas / drop
 * handler / context menu receive `NodeType` strings from untyped
 * sources — drag-and-drop payloads, context-menu callbacks, paste
 * payloads from the clipboard, etc. An unknown type must NOT crash
 * the canvas; it should be rejected up front so the rest of the
 * workflow keeps working.
 *
 * phase : added so the drop handler can refuse an
 * empty / corrupt drag payload instead of silently spawning a node
 * that React Flow can't render.
 */
export function isKnownNodeType(t: unknown): t is NodeType {
  // 1. Manifest is the source of truth at runtime — check it first
  //    so newly-added types are picked up without restarting the
  //    dev server. `nodeTypesManifest()` throws if the fetch hasn't
  //    resolved yet; swallow it and fall through to the static
  //    union fallback below.
  try {
    const m = nodeTypesManifest()
    if (m && m.entries && m.entries[t as NodeType]) return true
    if (m && Array.isArray(m.types) && m.types.includes(t as NodeType)) return true
  } catch {
    /* fall through */
  }
  // 2. Static union fallback — the generated `NodeType` literal
  //    union. Drift between this and the manifest is caught by
  //    `scripts/check_node_types_consistency.py`. Use a `Set` to
  //    keep the O(1) lookup without pulling another import.
  return (KNOWN_NODE_TYPES as readonly string[]).includes(t as string)
}

/** Snapshot of the generated union (kept here so the runtime check
 *  doesn't need to import a type-only value just for the lookup).
 *  Mirror of `GeneratedNodeType` in `types/workflow.generated.ts`. */
const KNOWN_NODE_TYPES: readonly NodeType[] = [
  'agent',
  // : `router` + `condition` collapsed to `branch`
  // — mode discriminator lives in `config.mode`.
  'branch',
  // : `parallel` + `steps` collapsed to `flow`
  // — mode discriminator lives in `config.mode`.
  'flow',
  'loop',
  // : `human_input` → `ask` (kind=control_flow).
  'ask',
  // : `http` + `mcp` + `tools` collapsed to `tool`
  // — source discriminator (`mcp` | `http` | `function`) lives in
  // `config.source`. : the 5 preset tool
  // types (wikipedia / tavily_search / duckduckgo / calculator /
  // arxiv_search) collapsed into the `tool` node's `preset` config
  // discriminator — they no longer appear as separate `NodeType`
  // values.
  'tool',
]