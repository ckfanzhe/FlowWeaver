/**
 * Tests for `isConfigurable` — drives whether left-click on a node
 * auto-opens the PropertyPanel.
 *
 * Regression history: wikipedia was added as a preset
 * extending `tool` (was `http` before the tool-source collapse),
 * with a registered `ToolForm` (was `HttpForm`) and a colour/icon
 * in the manifest. But the canvas's left-click handler gated on a
 * hand-maintained `CONFIGURABLE_TYPES` set that listed only the
 * base types. Left-click on a wikipedia node silently did nothing;
 * right-click (which goes through a different code path) still
 * worked, so the bug looked like an inconsistency between input
 * methods. Fix: derive `isConfigurable` from `FORM_REGISTRY` +
 * manifest's preset inheritance so every preset with a resolvable
 * form is configurable by construction.
 *
 * Tool-source collapse: wikipedia no longer has its own NodeType
 * literal — it collapsed into `tool` + `preset='wikipedia'`. The
 * pre-collapse wikipedia preset is gone from the live manifest;
 * this test fixture keeps a legacy `wikipedia` entry to pin the
 * original regression (preset types must remain configurable).
 *
 * Run:  npx tsx --test src/store/__tests__/isConfigurable.test.ts
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import { isConfigurable } from '../workflowStore'
import {
  _resetNodeTypesManifestCache,
  fetchNodeTypesManifest,
} from '../../api/nodeTypes'
import type { NodeTypesManifest } from '../../api/nodeTypes'

// ─────────────────────────────────────────────────────────────
// Test manifest shaped like the live one: 6 base types + the
// legacy wikipedia preset (extends: "tool").
// Compound-node collapse: `parallel`+`steps` → `flow`,
// `router`+`condition` → `branch`.
// Tool-source collapse: `http`+`mcp`+`tools` → `tool`.
// Preset collapse: the 5 preset tool types collapsed into
// `tool` + `preset` config discriminator — the wikipedia entry
// here is purely a back-compat test fixture.
// ─────────────────────────────────────────────────────────────

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
    tool:        makeEntry('tool_source'),
    // Legacy wikipedia entry — kept as a back-compat test fixture
    // (legacy preset envelopes still arrive this way). Live
    // manifests do NOT include a wikipedia row.
    wikipedia:   makeEntry('tool_source', 'tool'),
  },
}

function makeEntry(
  kind: 'executable' | 'compound' | 'tool_source',
  extendsName: string | null = null,
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
      isToolSource: false,
      needsToolWiring: false,
      skipPass1: false,
      stepWrapper: 'none',
    },
    defaultConfig: {},
    io: { inputs: [], outputs: [], tools: [] },
  }
}

// Stub `fetchNodeTypesManifest` so the test doesn't hit the network.
// The store calls `nodeTypesManifest()` synchronously after the App
// prefetches; we mimic that by populating the cache directly via the
// helper below.
function primeManifestCache(m: NodeTypesManifest): void {
  // nodeTypes.ts module-scope cache — we can't import it directly
  // without breaking the API surface, but the test reset hook
  // plus a tiny shim via `fetchNodeTypesManifest` works because
  // `fetchNodeTypesManifest` populates the cache on first call.
  _resetNodeTypesManifestCache()
  // Intercept by patching fetch — simpler: just install the
  // manifest via a one-shot fetch shim.
  const originalFetch = globalThis.fetch
  globalThis.fetch = (async () =>
    new Response(JSON.stringify(m), { status: 200 })) as typeof fetch
  // Fire-and-forget; the store calls `nodeTypesManifest()` lazily
  // and the cache will be populated after the microtask resolves.
  void fetchNodeTypesManifest().finally(() => {
    globalThis.fetch = originalFetch
  })
}

// ─────────────────────────────────────────────────────────────
// Tests
// ─────────────────────────────────────────────────────────────

test('isConfigurable: all 6 base types are configurable (manifest-free)', () => {
  // These don't need the manifest — they resolve through the
  // direct FORM_REGISTRY hit. Works even before the App effect
  // has run.
  // Compound-node collapse: 5 types collapsed to 2 (`parallel` +
  // `steps` → `flow`; `router` + `condition` → `branch`).
  // Tool-source collapse: http + mcp + tools → tool. The base
  // type count went 9 → 6.
  for (const t of [
    'agent', 'branch', 'flow', 'loop',
    'ask', 'tool',
  ] as const) {
    assert.equal(isConfigurable(t), true, `${t} should be configurable`)
  }
})

test('isConfigurable: wikipedia preset is configurable after manifest loads (regression)', () => {
  // Preset collapse: wikipedia is no longer a NodeType literal
  // in the generated union — but the fixture keeps a legacy
  // wikipedia entry to pin the original regression (preset types
  // must remain configurable). The `as never` cast matches the
  // current union.
  primeManifestCache(testManifest)
  // `fetchNodeTypesManifest` is async — wait one microtask flush.
  return new Promise<void>((resolve) => {
    setImmediate(() => {
      assert.equal(
        isConfigurable('wikipedia' as never),
        true,
        'wikipedia preset extends tool which has ToolForm — must be configurable',
      )
      resolve()
    })
  })
})

test('isConfigurable: unknown type is not configurable', () => {
  // No manifest entry, no FORM_REGISTRY hit → false.
  assert.equal(isConfigurable('mystery' as never), false)
})

test('isConfigurable: works after manifest fetch fails (graceful fallback)', () => {
  _resetNodeTypesManifestCache()
  const originalFetch = globalThis.fetch
  globalThis.fetch = (async () =>
    new Response('boom', { status: 500 })) as typeof fetch
  return new Promise<void>((resolve) => {
    setImmediate(() => {
      // Direct hits on the base types must still work — the
      // fallback path bypasses the manifest.
      assert.equal(isConfigurable('agent'), true)
      assert.equal(isConfigurable('tool'), true)
      // Wikipedia would normally be configurable via the manifest,
      // but with no manifest it falls back to the direct check
      // and returns false. This is intentional: better to refuse
      // silently than crash with "manifest not loaded".
      assert.equal(isConfigurable('wikipedia' as never), false)
      globalThis.fetch = originalFetch
      _resetNodeTypesManifestCache()
      resolve()
    })
  })
})