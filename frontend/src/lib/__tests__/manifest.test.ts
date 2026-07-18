/**
 * Tests for `walkExtends` — the shared `extends`-chain walker
 * consumed by `resolveForm` and `resolveVisual`.
 *
 * phase . The walker used to live as a copy-pasted
 * while-loop in both `forms/registry.ts` and `Nodes/nodeStyles.ts`;
 * the unified implementation is here in `lib/manifest.ts` and the
 * two consumers now share it. These tests pin the contract.
 *
 * Run:  npx tsx --test src/lib/__tests__/manifest.test.ts
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import { walkExtends, MAX_EXTENDS_DEPTH } from '../manifest'
import type { NodeTypesManifest } from '../../api/nodeTypes'

function makeManifest(chains: Record<string, string | null>): NodeTypesManifest {
  const entries: Record<string, unknown> = {}
  for (const [name, parent] of Object.entries(chains)) {
    entries[name] = {
      kind: 'executable',
      category: 'executable',
      extends: parent,
      displayName: name,
      i18nKey: name,
      color: '',
      textColor: '',
      icon: 'AgentIcon',
      paletteOrder: 0,
      ui: { group: 'Core', form: 'AgentForm', paletteOrder: 0 },
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
  return {
    schemaVersion: 2,
    types: Object.keys(chains),
    entries: entries as NodeTypesManifest['entries'],
  }
}

test('walkExtends: returns first ancestor the visit finds', () => {
  const m = makeManifest({ c: 'b', b: 'a', a: null })
  const got = walkExtends('c', m, (name) => `hit-${name}`)
  assert.equal(got, 'hit-b')
})

test('walkExtends: visits ancestors in depth-first order', () => {
  const m = makeManifest({ a: null, b: 'a', c: 'b' })
  const visited: string[] = []
  walkExtends('c', m, (name) => {
    visited.push(name)
    return null // keep walking
  })
  assert.deepEqual(visited, ['b', 'a'])
})

test('walkExtends: 3-level extends chain (phase regression)', () => {
  // a → b → c — make sure the depth bound doesn't accidentally
  // cap at 2 levels. The fixture covers a future preset-of-preset
  // that adds a third tier.
  const m = makeManifest({ a: 'b', b: 'c', c: null })
  const got = walkExtends('a', m, (name) => `hit-${name}`)
  assert.equal(got, 'hit-b', 'walk must reach the second-tier parent')
  // Walking one more step should also work.
  const walked = walkExtends('a', m, () => null)
  // null chain end → null result, but the walk itself must not crash.
  assert.equal(walked, null)
})

test('walkExtends: returns null when chain ends without a hit', () => {
  const m = makeManifest({ a: 'b', b: 'c', c: null })
  const got = walkExtends('a', m, () => null)
  assert.equal(got, null)
})

test('walkExtends: respects maxDepth and stops at the bound', () => {
  // Long chain: a → b → c → d → e → f. With maxDepth=2, only
  // a → b → c should be reachable.
  const m = makeManifest({
    a: 'b', b: 'c', c: 'd', d: 'e', e: 'f', f: null,
  })
  const visited: string[] = []
  walkExtends(
    'a',
    m,
    (name) => {
      visited.push(name)
      return null
    },
    2,
  )
  assert.deepEqual(visited, ['b', 'c'])
})

test('walkExtends: handles cycles without infinite-looping', () => {
  // a → b → a (cycle). The depth bound (default 8) prevents
  // infinite recursion. The backend's startup check rejects cycles
  // at load time, but the frontend can't depend on that.
  const m = makeManifest({ a: 'b', b: 'a' })
  const visited: string[] = []
  const got = walkExtends('a', m, (name) => {
    visited.push(name)
    return null
  })
  assert.ok(visited.length <= MAX_EXTENDS_DEPTH, `walked ${visited.length} steps`)
  assert.equal(got, null)
})

test('walkExtends: visit returning 0 / "" / false still short-circuits', () => {
  // The visit result is checked with `!== null && !== undefined`
  // — falsy non-nullish values (0, "", false) are valid hits. The
  // consumer's lookup-table result type is `T | null`, so this
  // matters mainly for primitive T.
  const m = makeManifest({ a: 'b', b: null })
  assert.equal(walkExtends('a', m, () => 0), 0)
  assert.equal(walkExtends('a', m, () => ''), '')
  assert.equal(walkExtends('a', m, () => false), false)
})