/**
 * Tests for the layout helpers (Node 20+ built-in test runner via tsx).
 *
 * Run:  npx tsx --test src/lib/layout.test.ts
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import type { WorkflowNode } from '../types/workflow'
import { spreadNodes } from './layout'

function n(id: string, x: number, y: number): WorkflowNode {
  return {
    id,
    type: 'agent',
    position: { x, y },
    data: { label: id, config: {} },
  }
}

function boxesOverlap(
  results: Array<{ position: { x: number; y: number } }>,
  minDx: number,
  minDy: number,
): Array<[number, number]> {
  const out: Array<[number, number]> = []
  for (let i = 0; i < results.length; i++) {
    for (let j = i + 1; j < results.length; j++) {
      const a = results[i].position
      const b = results[j].position
      if (Math.abs(a.x - b.x) < minDx && Math.abs(a.y - b.y) < minDy) {
        out.push([i, j])
      }
    }
  }
  return out
}

test('empty input → empty output', () => {
  assert.deepEqual(spreadNodes([]), [])
})

test('single node → unchanged', () => {
  const node = n('a', 100, 100)
  const out = spreadNodes([node])
  assert.equal(out.length, 1)
  assert.equal(out[0].moved, false)
  assert.deepEqual(out[0].position, { x: 100, y: 100 })
})

test('already-spaced layout is left alone', () => {
  const nodes = [
    n('a', 0, 0),
    n('b', 300, 0),    // > minDx apart
    n('c', 0, 200),    // > minDy apart
  ]
  const out = spreadNodes(nodes)
  assert.equal(out.every((r) => r.moved === false), true)
})

test('two nodes stacked at origin get pushed apart', () => {
  const out = spreadNodes([n('a', 0, 0), n('b', 0, 0)])
  assert.equal(out.length, 2)
  // No overlap remaining
  assert.equal(boxesOverlap(out, 240, 120).length, 0)
  // Both reported as moved
  assert.equal(out.every((r) => r.moved === true), true)
})

test('five nodes stacked at origin all get separated', () => {
  const nodes = Array.from({ length: 5 }, (_, i) => n(`n${i}`, 0, 0))
  const out = spreadNodes(nodes)
  assert.equal(boxesOverlap(out, 240, 120).length, 0, 'no pair should overlap')
  // All five must have moved (none were alone)
  assert.equal(out.filter((r) => r.moved).length, 5)
})

test('horizontally-stacked nodes separate vertically', () => {
  // 3 nodes at the same y but crammed along x
  const nodes = [n('a', 0, 50), n('b', 10, 50), n('c', 20, 50)]
  const out = spreadNodes(nodes)
  assert.equal(boxesOverlap(out, 240, 120).length, 0)
})

test('diagonally-overlapping cluster is resolved', () => {
  // A 2×2 grid of nodes all overlapping both axes
  const nodes = [
    n('a', 0, 0),
    n('b', 50, 0),
    n('c', 0, 50),
    n('d', 50, 50),
  ]
  const out = spreadNodes(nodes)
  assert.equal(boxesOverlap(out, 240, 120).length, 0)
})

test('does not mutate input positions', () => {
  const nodes = [n('a', 0, 0), n('b', 0, 0), n('c', 0, 0)]
  const original = nodes.map((nd) => ({ ...nd.position }))
  spreadNodes(nodes)
  for (let i = 0; i < nodes.length; i++) {
    assert.deepEqual(nodes[i].position, original[i])
  }
})

test('options.minDx / minDy are respected', () => {
  // With tiny minimums, even badly-overlapping nodes are considered OK
  const out = spreadNodes([n('a', 0, 0), n('b', 5, 5)], { minDx: 1, minDy: 1 })
  assert.equal(out.every((r) => r.moved === false), true)
})