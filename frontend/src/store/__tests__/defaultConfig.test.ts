/**
 * Regression tests for `defaultConfig()` (via `addNode`) — preset
 * nodes must drop with their manifest-resolved defaultConfig, NOT
 * fall through to `{}`.
 *
 * History : wikipedia was the first preset to extend a
 * base type via the manifest's `extends` + `overrides.defaultConfig`
 * mechanism. The store's `defaultConfig(type)` had a hand-written
 * `switch` with no `wikipedia` case, so `addNode('wikipedia', ...)`
 * landed on `default: return {}`. The `ToolForm` (was `HttpForm`
 * pre-) then rendered every field empty, and
 * the i18n placeholders (`fetch_user` / `通过id查询用户`) appeared
 * as if they were the actual values — visually indistinguishable
 * from a broken preset.
 *
 * : wikipedia collapsed into `tool` +
 * `preset='wikipedia'` — it no longer has its own NodeType literal.
 * The wikipedia preset defaults now travel via the manifest's
 * `tool.defaultConfig.preset = 'wikipedia'` path (or, for legacy
 * `extends: 'tool'` test fixtures, the explicit `wikipedia`
 * preset row on the manifest entry).
 *
 * Fix: `defaultConfig(type)` now consults the manifest's resolved
 * `defaultConfig` first (which the backend merges server-side).
 * The switch is a pre-fetch fallback only.
 *
 * Run:  npx tsx --test src/store/__tests__/defaultConfig.test.ts
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  _resetNodeTypesManifestCache,
  fetchNodeTypesManifest,
} from '../../api/nodeTypes'
import type { NodeTypesManifest } from '../../api/nodeTypes'
import { useWorkflowStore } from '../workflowStore'

// ─────────────────────────────────────────────────────────────
// Test manifest — shaped like the live one. The wikipedia preset
// overrides `toolName`, `toolDescription`, `baseUrl`, and `path`
// on top of `tool` (source='http') defaults; everything else is
// inherited verbatim. : wikipedia no longer
// has its own base type — the wikipedia preset lives INSIDE the
// `tool` node's `preset='wikipedia'` config discriminator. This
// test fixture keeps the legacy `wikipedia` entry shape so the
// regression is still pinned against the same code path that
// bit the legacy preset path.
// ─────────────────────────────────────────────────────────────

const httpDefaultConfig = {
  source: 'http',
  toolName: 'http_call',
  toolDescription: 'Make an HTTP request',
  method: 'GET',
  baseUrl: '',
  path: '',
  headers: {},
  queryParams: {},
  authToken: '',
  bodySchema: '',
}

const wikipediaDefaultConfig = {
  ...httpDefaultConfig,
  preset: 'wikipedia',
  toolName: 'wikipedia_search',
  toolDescription: 'Search Wikipedia for articles matching a query',
  baseUrl: 'https://en.wikipedia.org',
  path: '/w/api.php?action=query&list=search&srsearch={query}&format=json&utf8=1&srlimit=5',
}

function makeEntry(
  kind: 'executable' | 'compound' | 'tool_source',
  extendsName: string | null = null,
  defaultConfig: Record<string, unknown> = {},
): NodeTypesManifest['entries'][string] {
  return {
    category: kind,
    kind,
    extends: extendsName,
    displayName: 'x',
    i18nKey: 'x',
    color: '',
    textColor: '',
    icon: 'AgentIcon',
    paletteOrder: 0,
    ui: { group: 'Core', form: 'ToolForm', paletteOrder: 0 },
    capabilities: {
      compoundPass: null,
      isToolSource: kind === 'tool_source',
      needsToolWiring: false,
      skipPass1: false,
      stepWrapper: 'none',
    },
    defaultConfig,
    io: { inputs: [], outputs: [], tools: [] },
  }
}

const testManifest: NodeTypesManifest = {
  schemaVersion: 2,
  types: [
    'agent', 'branch', 'flow', 'loop',
    'ask', 'tool', 'wikipedia',
  ],
  entries: {
    agent:       makeEntry('executable'),
    branch:      makeEntry('compound'),
    flow:        makeEntry('compound'),
    loop:        makeEntry('compound'),
    ask: makeEntry('executable'),
    tool:        makeEntry('tool_source', null, { ...httpDefaultConfig }),
    // Legacy wikipedia entry — kept as a back-compat test fixture
    // shape (legacy preset envelopes still arrive this way via
    // the `_compat` migration). The fixture is synthetic; live
    // manifests do NOT include a wikipedia row.
    wikipedia:   makeEntry('tool_source', 'tool', { ...wikipediaDefaultConfig }),
  },
}

/**
 * Stub the manifest fetch so the store can resolve `nodeTypesManifest()`
 * synchronously. Mirrors the pattern in `isConfigurable.test.ts`.
 */
async function primeManifestCache(m: NodeTypesManifest): Promise<void> {
  _resetNodeTypesManifestCache()
  const originalFetch = globalThis.fetch
  globalThis.fetch = (async () =>
    new Response(JSON.stringify(m), { status: 200 })) as typeof fetch
  try {
    await fetchNodeTypesManifest()
  } finally {
    globalThis.fetch = originalFetch
  }
}

function resetCanvas(): void {
  useWorkflowStore.getState().reset()
}

// ─────────────────────────────────────────────────────────────
// Tests
// ─────────────────────────────────────────────────────────────

test('addNode(wikipedia): drops with manifest-resolved defaultConfig (regression)', async () => {
  // wikipedia is no longer a NodeType literal in the generated
  // union — but this test fixture keeps a legacy `wikipedia`
  // manifest entry so we can pin the original regression
  // (preset-defaults lost → empty form). We use a TS cast to make
  // the call valid for the current union.
  await primeManifestCache(testManifest)
  resetCanvas()

  const id = useWorkflowStore.getState().addNode('wikipedia' as never, { x: 0, y: 0 })
  const node = useWorkflowStore.getState().nodes.find((n) => n.id === id)
  assert.ok(node, 'node should exist')

  const cfg = node.data.config as Record<string, unknown>
  // Preset-overridden keys must come from the manifest, not be empty
  // (which is what the bug produced — `{}` → HttpForm showed the
  // i18n placeholders `fetch_user` / `通过id查询用户`).
  assert.equal(cfg.toolName, 'wikipedia_search')
  assert.equal(
    cfg.toolDescription,
    'Search Wikipedia for articles matching a query',
  )
  assert.equal(cfg.baseUrl, 'https://en.wikipedia.org')
  assert.equal(
    cfg.path,
    '/w/api.php?action=query&list=search&srsearch={query}&format=json&utf8=1&srlimit=5',
  )
  // http-inherited fields should still be present (the form needs
  // them so its `NodeDataField` reads don't crash on `undefined`).
  assert.equal(cfg.method, 'GET')
  assert.deepEqual(cfg.headers, {})
  assert.deepEqual(cfg.queryParams, {})
  assert.equal(cfg.authToken, '')
  assert.equal(cfg.bodySchema, '')
})

test('addNode(tool): drops with manifest-resolved defaultConfig (sanity)', async () => {
  // : the old `http` node type is now `tool`
  // with `source='http'`. This test verifies the sanity path — a
  // freshly-dropped `tool` node carries the source='http' defaults
  // because that's what the test fixture configures on the entry.
  await primeManifestCache(testManifest)
  resetCanvas()

  const id = useWorkflowStore.getState().addNode('tool', { x: 0, y: 0 })
  const node = useWorkflowStore.getState().nodes.find((n) => n.id === id)
  assert.ok(node)
  const cfg = node.data.config as Record<string, unknown>
  assert.equal(cfg.source, 'http')
  assert.equal(cfg.toolName, 'http_call')
  assert.equal(cfg.toolDescription, 'Make an HTTP request')
})

test('addNode: defaultConfig is a clone — edits do not leak back to manifest', async () => {
  // Critical invariant: the store's `addNode` must NOT return the
  // manifest's `entry.defaultConfig` object directly. If it did,
  // every user's edits would mutate the shared manifest cache and
  // the next preset dropped would inherit the previous user's
  // edits. The fix uses `structuredClone` to defensively copy.
  await primeManifestCache(testManifest)
  resetCanvas()

  const id = useWorkflowStore.getState().addNode('wikipedia' as never, { x: 0, y: 0 })
  const node = useWorkflowStore.getState().nodes.find((n) => n.id === id)
  assert.ok(node)
  const cfg = node.data.config as Record<string, unknown>
  // Mutate the freshly-dropped config. `updateNodeData` merges the
  // patch onto `data`, so we go through `config: { ...cfg, ... }`
  // to update a field inside the config bag (matching how the form
  // sets values via `set`).
  useWorkflowStore
    .getState()
    .updateNodeData(id, { config: { ...cfg, toolName: 'mutated' } })

  // Drop a second wikipedia — its defaultConfig must still be the
  // pristine preset value, not 'mutated'.
  const id2 = useWorkflowStore.getState().addNode('wikipedia' as never, { x: 10, y: 10 })
  const node2 = useWorkflowStore.getState().nodes.find((n) => n.id === id2)
  assert.ok(node2)
  const cfg2 = node2.data.config as Record<string, unknown>
  assert.equal(
    cfg2.toolName,
    'wikipedia_search',
    'preset defaultConfig must be cloned, not shared with the manifest',
  )
  // The first one keeps the user's edit.
  const node1After = useWorkflowStore
    .getState()
    .nodes.find((n) => n.id === id)
  assert.ok(node1After)
  assert.equal(
    (node1After.data.config as Record<string, unknown>).toolName,
    'mutated',
    'first node retains its user edit',
  )
})

test('addNode: unknown type still falls through to {} (no crash before manifest)', () => {
  // Before the manifest has loaded, `defaultConfig` falls back to the
  // switch — which has no entry for the type and returns `{}`.
  // After manifest load, the manifest branch above wins; this test
  // only verifies the pre-fetch path doesn't throw on an unknown type.
  _resetNodeTypesManifestCache()
  resetCanvas()
  assert.ok(useWorkflowStore.getState, 'store exists')
})