/**
 * Regression test: `customNodeTypes` registry must include every
 * `NodeType` defined by the manifest.
 *
 * Background : wikipedia was the first preset added to
 * the manifest. It shipped with a distinct colour and a dedicated
 * WikipediaNode wrapper that the registry initially forgot to
 * register — React Flow fell back to its default unstyled white
 * rectangle for any wikipedia node. This test pins the contract
 * that every shipped NodeType has a registry entry, so the same
 * drift can never happen again.
 *
 * : wikipedia + tavily_search + duckduckgo +
 * calculator + arxiv_search collapsed into the unified `tool`
 * node's `preset` config discriminator — they no longer appear as
 * separate NodeType literals in the manifest, so they don't need
 * `customNodeTypes` entries either. The registry now exposes 6
 * base types.
 *
 * Why we don't iterate the manifest directly:
 *   - `nodeTypes.ts` exports `fetchNodeTypesManifest()` which is an
 *     async fetch; pulling it in here would require a fetch mock.
 *   - The set of shipped node types is also already encoded in the
 *     generated `NodeType` union (`types/workflow.generated.ts`),
 *     which itself is derived from the manifest by
 *     `scripts/generate_node_types.py` — so checking against that
 *     union catches both kinds of drift: manifest↔generated and
 *     generated↔registry.
 *
 * Run:  npx tsx --test src/components/Nodes/__tests__/customNodeTypes.test.ts
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import type { NodeType } from '../../../types/workflow'
import { customNodeTypes } from '../index'

// All NodeTypes that the runtime knows about — this comes from the
// generated union which is regenerated from the manifest.
// Node-type collapses reflected in this list:
//   - `parallel` + `steps` → `flow`
//   - `router` + `condition` → `branch`
//   - 5 preset tool types → `tool` via `preset` config discriminator
//     (gone from this list)
//   - http + mcp + tools → `tool`
//   - human_input → `ask`
const ALL_TYPES: NodeType[] = [
  'agent',
  'branch',
  'flow',
  'loop',
  'ask',
  'tool',
]

test('customNodeTypes: every NodeType is registered', () => {
  for (const t of ALL_TYPES) {
    assert.ok(
      customNodeTypes[t],
      `customNodeTypes is missing an entry for ${JSON.stringify(t)}; ` +
        'React Flow renders an unstyled rectangle for unregistered types',
    )
    assert.equal(typeof customNodeTypes[t], 'function')
  }
})

test('customNodeTypes: flow has its own component (regression)', () => {
  // : `flow` replaces the prior `parallel` and
  // `steps` types. Without a `flow` entry in `customNodeTypes`,
  // React Flow draws a plain unstyled white rectangle when the
  // user drops a flow node — same drift that bit wikipedia earlier.
  assert.ok(
    customNodeTypes.flow,
    'customNodeTypes.flow is required — without it, React Flow ' +
      'draws a plain white rectangle when the user drops a flow node',
  )
  assert.equal(typeof customNodeTypes.flow, 'function')
})

test('customNodeTypes: tool has its own component (preset badge renders inside it)', () => {
  // : the 5 preset tool types (wikipedia /
  // tavily_search / duckduckgo / calculator / arxiv_search) collapsed
  // into the `tool` node's `preset` config discriminator. There is
  // NO separate component per preset — the single `ToolNode` body
  // renders a preset badge when `cfg.preset` is set. So the
  // registry entry that matters is `tool`, not a per-preset
  // wrapper. Pinning it catches the same "white rectangle"
  // regression that bit the wikipedia preset on early merges.
  assert.ok(
    customNodeTypes.tool,
    'customNodeTypes.tool is required — without it, React Flow draws ' +
      'a plain white rectangle when the user drops a tool (or any of ' +
      'the 5 collapsed presets) node',
  )
  assert.equal(typeof customNodeTypes.tool, 'function')
})

test('customNodeTypes: registry has no extra entries', () => {
  // Catches the inverse drift: a stale entry that points to a removed
  // node type. Both directions are bugs.
  const registered = Object.keys(customNodeTypes).sort()
  assert.deepEqual(registered, [...ALL_TYPES].sort())
})