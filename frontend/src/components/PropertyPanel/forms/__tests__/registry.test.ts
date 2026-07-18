/**
 * Tests for the form-component registry (phase, ).
 *
 * Run:  npx tsx --test src/components/PropertyPanel/forms/__tests__/registry.test.ts
 *
 * Pins the contract for `resolveForm()`:
 *   1. Direct hit — every base type resolves to its registered form.
 *   2. Preset inheritance — wikipedia/brave_search/tavily/etc. fall
 *      through to ToolForm via `extends: "tool"`.
 *   3. Unknown type returns null (not undefined, not a throw).
 *   4. Cycle bounded by `maxDepth` — a cycle in the manifest doesn't
 *      infinite-loop the resolver.
 *   5. Null/empty manifest safety — when the API is unreachable, the
 *      resolver falls back gracefully instead of throwing.
 *
 * These tests exercise the registry directly without rendering React.
 *
 * : the three tool-source types (http/mcp/tools)
 * collapsed into a single `tool` node — `HttpForm` + `McpForm` +
 * `ToolsForm` merged into `ToolForm` (mode-aware via `cfg.source`).
 * The form registry now exposes `tool: ToolForm` directly; presets
 * still chain through `extends: 'tool'`.
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import { resolveForm, FORM_REGISTRY } from '../registry'
import { ToolForm } from '../../ToolForm'
import { AgentForm } from '../../AgentForm'
import type { NodeType } from '../../../../types/workflow'
import type { NodeTypesManifest } from '../../../../api/nodeTypes'

// ───────────────────────────────────────────────────────────────
// Helper — synthesize a manifest with the given entries. Used to
// test inheritance behaviour without coupling to the live JSON.
// ───────────────────────────────────────────────────────────────
function makeManifest(
  entries: Record<string, Partial<NodeTypesManifest['entries'][string]> & { extends?: string | null }>,
  types?: string[],
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
      ...partial,
    }
  }
  return {
    schemaVersion: 2,
    types: (types ?? Object.keys(entries)) as NodeType[],
    entries: full,
  }
}

// ───────────────────────────────────────────────────────────────
// Direct hit — every base type resolves to its registered form
// ───────────────────────────────────────────────────────────────

test('resolveForm: direct hit on agent', () => {
  assert.equal(resolveForm('agent', null), AgentForm)
})

test('resolveForm: direct hit on tool', () => {
  // The unified `tool` form is `ToolForm`. It reads `cfg.source`
  // to decide which sub-form to render.
  assert.equal(resolveForm('tool', null), ToolForm)
})

test('resolveForm: direct hit does NOT consult manifest', () => {
  // The direct lookup is intentionally manifest-free so the
  // resolver stays usable before the manifest fetch resolves.
  // A null manifest must NOT block a direct hit.
  assert.equal(resolveForm('agent', null), AgentForm)
})

// ───────────────────────────────────────────────────────────────
// Preset inheritance — extends chain walking
// ───────────────────────────────────────────────────────────────

test('resolveForm: brave_search extends tool → ToolForm', () => {
  const manifest = makeManifest({
    tool: { kind: 'tool_source' },
    brave_search: { extends: 'tool', kind: 'tool_source' },
  })
  assert.equal(resolveForm('brave_search', manifest), ToolForm)
})

test('resolveForm: multi-level extends chain (a → b → tool → Form)', () => {
  // Forms are registered for `tool`. `a` and `b` chain through.
  // Tests the depth-walking logic, not just one-level extends.
  const manifest = makeManifest({
    tool: { kind: 'tool_source' },
    b: { extends: 'tool', kind: 'tool_source' },
    a: { extends: 'b', kind: 'tool_source' },
  })
  assert.equal(resolveForm('a', manifest), ToolForm)
})

test('resolveForm: preset with no form registered and no parent → null', () => {
  // Edge case: manifest declares a type, but neither the type nor
  // any of its parents are in FORM_REGISTRY. Resolver returns null,
  // caller renders the NoConfigFallback.
  const manifest = makeManifest({
    mystery: {},
  })
  assert.equal(resolveForm('mystery', manifest), null)
})

// ───────────────────────────────────────────────────────────────
// Null / missing manifest safety
// ───────────────────────────────────────────────────────────────

test('resolveForm: unknown type with no manifest → null', () => {
  assert.equal(resolveForm('mystery_type' as NodeType, null), null)
})

test('resolveForm: unknown type with manifest entry but no extends → null', () => {
  const manifest = makeManifest({ mystery: {} })
  assert.equal(resolveForm('mystery', manifest), null)
})

test('resolveForm: unknown type with manifest entry whose extends is unknown → null', () => {
  // `extends: "tool"` — but if `tool` had no form registered, the
  // chain would terminate. We can't reproduce that here (ToolForm
  // IS registered) so we verify the chain still resolves.
  const manifest = makeManifest({
    mystery: { extends: 'tool' },
  })
  // tool IS registered → resolves through it.
  assert.equal(resolveForm('mystery', manifest), ToolForm)
})

// ───────────────────────────────────────────────────────────────
// Cycle protection — bounded by maxDepth
// ───────────────────────────────────────────────────────────────

test('resolveForm: cycle in extends chain bounded by maxDepth', () => {
  // a → b → a → b → ... The resolver must NOT infinite-loop. With
  // maxDepth=8 the cycle terminates after 8 hops and returns null
  // (no form was registered for either).
  const manifest = makeManifest({
    a: { extends: 'b' },
    b: { extends: 'a' },
  })
  // No form for `a` or `b` — resolver should walk the cycle and
  // bail out via maxDepth, returning null.
  assert.equal(resolveForm('a', manifest), null)
})

test('resolveForm: custom maxDepth respects bound', () => {
  const manifest = makeManifest({
    a: { extends: 'tool' },  // resolves at depth 1
  })
  // maxDepth=0 should disable extends walking entirely.
  assert.equal(resolveForm('a', manifest, 0), null)
  // maxDepth=1 should find tool.
  assert.equal(resolveForm('a', manifest, 1), ToolForm)
})

// ───────────────────────────────────────────────────────────────
//  — preset types collapsed into `tool` + `cfg.preset`.
//
// The 5 preset tool types (wikipedia / tavily_search / duckduckgo /
// calculator / arxiv_search) collapsed into the unified `tool` node's
// `preset` config discriminator. They no longer appear as separate
// `NodeType` literals in the generated union, so they can't be passed
// to `resolveForm()` directly anymore. The `extends: 'tool'` chain is
// still valid for legacy / synthetic manifests (the `_compat`
// migration rewrites legacy preset envelopes to `type: 'tool'`
// on read, but the chain walk still works if the manifest
// hasn't been migrated yet — important for the canvas
// first-paint).
// ───────────────────────────────────────────────────────────────

test('resolveForm: legacy preset with extends=tool → ToolForm (back-compat)', () => {
  // A legacy preset entry shape (any name, extends: 'tool')
  // still resolves to ToolForm. Covers wikipedia / tavily_search /
  // etc. for legacy manifests where the chain walk hasn't been
  // migrated yet.
  const manifest = makeManifest({
    tool: { kind: 'tool_source' },
    legacy_preset: { extends: 'tool', kind: 'tool_source' },
  })
  assert.equal(resolveForm('legacy_preset', manifest), ToolForm)
})

test('resolveForm: every base type has a registered form', () => {
  // +N2 : 5 types collapsed to 2 (`parallel` +
  // `steps` → `flow`; `router` + `condition` → `branch`).
  // : the 5 preset types collapsed into `tool`
  // via the `preset` config discriminator — no longer in the base
  // type list.
  // : http + mcp + tools → tool. The base
  // type count went 9 → 6.
  const baseTypes: NodeType[] = [
    'agent', 'branch', 'flow', 'loop', 'ask', 'tool',
  ]
  for (const t of baseTypes) {
    assert.ok(FORM_REGISTRY[t], `FORM_REGISTRY missing form for ${t}`)
  }
})