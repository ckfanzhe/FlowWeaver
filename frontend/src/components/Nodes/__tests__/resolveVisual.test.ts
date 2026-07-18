/**
 * Tests for `resolveVisual()` — the extends-aware node visual lookup
 * (phase follow-up, ).
 *
 * Run:  npx tsx --test src/components/Nodes/__tests__/resolveVisual.test.ts
 *
 * Pins the contract for preset visual inheritance:
 *   1. Direct hit on a registered type returns its visual.
 *   2. Preset types (wikipedia / brave_search / …) inherit their
 *      parent's visual via the `extends:` chain in the manifest.
 *   3. An unknown type with no parent returns null (the caller falls
 *      back to `GENERIC_VISUAL` in BaseNode).
 *   4. The walk is bounded by `maxDepth` so a manifest cycle can't
 *      infinite-loop.
 *
 * Mirrors the same lookup rules as `resolveForm()` in
 * `PropertyPanel/forms/registry.ts`.
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import { resolveVisual, type NodeVisual } from '../nodeStyles'
import type { NodeTypesManifest } from '../../../api/nodeTypes'

// ───────────────────────────────────────────────────────────────
// Helpers
// ───────────────────────────────────────────────────────────────

function makeVisual(overrides: Partial<NodeVisual> = {}): NodeVisual {
  return {
    color: 'border-blue-400',
    text: 'text-blue-700',
    Icon: () => null,
    i18nKey: 'agent' as never,
    displayName: 'Agent',
    paletteOrder: 1,
    category: 'executable',
    ...overrides,
  }
}

function makeVisuals(types: string[]): Record<string, NodeVisual> {
  const out: Record<string, NodeVisual> = {}
  for (const t of types) {
    out[t] = makeVisual({ i18nKey: t as never, displayName: t })
  }
  return out
}

function makeManifest(
  entries: Record<string, { extends?: string | null }>,
): NodeTypesManifest {
  const full: NodeTypesManifest['entries'] = {}
  for (const [name, partial] of Object.entries(entries)) {
    full[name] = {
      category: 'executable',
      kind: 'executable',
      extends: partial.extends ?? null,
      displayName: name,
      i18nKey: name,
      color: '',
      textColor: '',
      icon: 'AgentIcon',
      paletteOrder: 0,
      ui: { group: 'Core', form: '', paletteOrder: 0 },
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
  return { schemaVersion: 2, types: Object.keys(entries), entries: full }
}

// ───────────────────────────────────────────────────────────────
// Direct hit
// ───────────────────────────────────────────────────────────────

test('resolveVisual: direct hit on a registered type', () => {
  const visuals = makeVisuals(['agent', 'tool'])
  const result = resolveVisual('agent', visuals, null)
  assert.equal(result?.i18nKey, 'agent')
})

test('resolveVisual: direct hit does NOT consult manifest', () => {
  // Direct lookups are manifest-free — the caller must keep working
  // before the manifest fetch resolves.
  const visuals = makeVisuals(['agent'])
  assert.ok(resolveVisual('agent', visuals, null))
})

// ───────────────────────────────────────────────────────────────
// Preset inheritance
// ───────────────────────────────────────────────────────────────

test('resolveVisual: wikipedia → tool visual via extends chain', () => {
  // wikipedia extends the merged `tool` node. The visual
  // chain walks wikipedia → tool.
  const visuals = makeVisuals(['agent', 'tool'])
  const manifest = makeManifest({
    tool: {},
    wikipedia: { extends: 'tool' },
  })
  const result = resolveVisual('wikipedia' as never, visuals, manifest)
  assert.equal(result?.i18nKey, 'tool')
})

test('resolveVisual: 6 preset HTTP types all resolve to tool visual', () => {
  // : presets chain through the merged
  // `tool` node (was `http`).
  const visuals = makeVisuals(['tool'])
  const presets = ['wikipedia', 'brave_search', 'tavily',
                   'open_meteo', 'coingecko', 'frankfurter']
  const manifest = makeManifest({
    tool: {},
    ...Object.fromEntries(presets.map((p) => [p, { extends: 'tool' }])),
  })
  for (const p of presets) {
    const result = resolveVisual(p as never, visuals, manifest)
    assert.equal(result?.i18nKey, 'tool', `${p} should resolve to tool visual`)
  }
})

// ───────────────────────────────────────────────────────────────
// Live wikipedia preset — the only shipped preset
// ───────────────────────────────────────────────────────────────

test('resolveVisual: wikipedia preset resolves to tool visual', () => {
  // : minimal live-shaped manifest:
  // tool + wikipedia (extends: tool).
  const visuals = makeVisuals(['tool'])
  const manifest = makeManifest({
    tool: {},
    wikipedia: { extends: 'tool' },
  })
  const result = resolveVisual('wikipedia' as never, visuals, manifest)
  assert.equal(result?.i18nKey, 'tool')
})

// ───────────────────────────────────────────────────────────────
// Icon registration — the manifest references icons by name, so
// every shipped icon MUST be exported AND registered in
// `ICON_BY_MANIFEST_NAME`. A missing icon throws at first render
// (entryToVisual raises) — the canvas would be broken.
// ───────────────────────────────────────────────────────────────

test('every shipped manifest icon has a matching component', async () => {
  const mod = await import('../NodeIcons')
  const map = mod.ICON_BY_MANIFEST_NAME as Record<string, unknown>
  // : `ParallelIcon` + `StepsIcon` collapsed to
  // a single `FlowIcon`.
  // : `RouterIcon` + `ConditionIcon` collapsed
  // to a single `BranchIcon`.
  // : `WikipediaIcon` + `TavilyIcon` +
  // `DuckDuckGoIcon` + `CalculatorIcon` + `ArxivIcon` all deleted —
  // the 5 collapsed presets use `ToolIcon` + body's preset badge.
  // : `McpIcon` + `HttpIcon` + `ToolsIcon`
  // collapsed to a single `ToolIcon`. The 6 icons the live manifest
  // currently references:
  const expected = [
    'AgentIcon', 'BranchIcon', 'FlowIcon',
    'LoopIcon', 'AskIcon', 'ToolIcon',
  ]
  for (const name of expected) {
    assert.ok(map[name], `ICON_BY_MANIFEST_NAME missing ${name}`)
  }
  // And the deleted preset icons are gone:
  assert.equal(
    map['WikipediaIcon'], undefined,
    'WikipediaIcon is no longer in ICON_BY_MANIFEST_NAME — wikipedia collapsed into tool+preset',
  )
})

test('resolveVisual: multi-level extends chain (a → b → c)', () => {
  const visuals = makeVisuals(['c'])
  const manifest = makeManifest({
    c: {},
    b: { extends: 'c' },
    a: { extends: 'b' },
  })
  assert.equal(resolveVisual('a' as never, visuals, manifest)?.i18nKey, 'c')
})

// ───────────────────────────────────────────────────────────────
// Unknown type safety
// ───────────────────────────────────────────────────────────────

test('resolveVisual: unknown type with no manifest → null', () => {
  const visuals = makeVisuals(['agent'])
  assert.equal(resolveVisual('mystery' as never, visuals, null), null)
})

test('resolveVisual: unknown type with manifest entry but no extends → null', () => {
  const visuals = makeVisuals([])
  const m = makeManifest({ mystery: {} })
  assert.equal(resolveVisual('mystery' as never, visuals, m), null)
})

test('resolveVisual: cycle bounded by maxDepth', () => {
  // a → b → a → b → ... Resolver MUST NOT infinite-loop.
  const visuals = makeVisuals([])
  const m = makeManifest({ a: { extends: 'b' }, b: { extends: 'a' } })
  // Neither `a` nor `b` is in `visuals`, so the walk exhausts and
  // returns null after `maxDepth` hops.
  assert.equal(resolveVisual('a' as never, visuals, m, 8), null)
})

test('resolveVisual: custom maxDepth respects bound', () => {
  const visuals = makeVisuals(['tool'])
  const m = makeManifest({ wikipedia: { extends: 'tool' } })
  // maxDepth=0 → no extends walking → null
  assert.equal(resolveVisual('wikipedia' as never, visuals, m, 0), null)
  // maxDepth=1 → finds tool
  assert.equal(resolveVisual('wikipedia' as never, visuals, m, 1)?.i18nKey, 'tool')
})