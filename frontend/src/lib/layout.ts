/**
 * Layout helpers for the canvas.
 *
 * `spreadNodes` resolves overlaps between workflow nodes by repulsion.
 * Each node is treated as an axis-aligned bounding box of size
 * `minDx × minDy` (default 240 × 120 px — a comfortable fit for the
 * BaseNode card plus a bit of margin). When two boxes overlap, we push
 * them apart along the axis where they were already more separated.
 *
 * The algorithm is iterative, cheap (O(n² · iterations)), and converges
 * within ~n/2 iterations even for fully-stacked inputs. It deliberately
 * leaves well-spaced layouts alone — only nodes that actually overlap
 * get moved.
 */
import type { WorkflowNode } from '../types/workflow'

export interface SpreadOptions {
  /** Minimum horizontal distance between two node centers. Default 240. */
  minDx?: number
  /** Minimum vertical distance between two node centers. Default 120. */
  minDy?: number
  /** Hard cap on iterations. Default 80. */
  maxIter?: number
}

export interface SpreadResult {
  id: string
  position: { x: number; y: number }
  /** True if this node moved during the spread. */
  moved: boolean
}

export function spreadNodes(
  nodes: WorkflowNode[],
  opts: SpreadOptions = {},
): SpreadResult[] {
  const minDx = opts.minDx ?? 240
  const minDy = opts.minDy ?? 120
  const maxIter = opts.maxIter ?? 80

  // Working copy of positions keyed by node id so we never mutate the
  // caller's objects until we return.
  const pos = new Map<string, { x: number; y: number }>()
  for (const n of nodes) pos.set(n.id, { x: n.position.x, y: n.position.y })

  // Pairwise repulsion. At each step we look at every unordered pair;
  // overlapping pairs get pushed symmetrically along the axis with the
  // SMALLER overlap (i.e. where they're already more separated, so the
  // push is least disruptive).
  for (let iter = 0; iter < maxIter; iter++) {
    let iterMoved = false
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = pos.get(nodes[i].id)!
        const b = pos.get(nodes[j].id)!
        const dx = a.x - b.x
        const dy = a.y - b.y
        const absDx = Math.abs(dx)
        const absDy = Math.abs(dy)
        // If EITHER axis is already clear of overlap, the boxes don't
        // intersect — leave them alone.
        if (absDx >= minDx || absDy >= minDy) continue

        const overlapX = minDx - absDx
        const overlapY = minDy - absDy

        let pushX = 0
        let pushY = 0
        if (absDx > absDy) {
          // More horizontal separation → resolve horizontally
          pushX = (overlapX / 2 + 1) * signOr(dx, j % 2 === 0 ? -1 : 1)
        } else {
          // More vertical separation (or equal) → resolve vertically.
          // Handles perfectly-stacked nodes (absDy=0) via the parity
          // tiebreaker in signOr.
          pushY = (overlapY / 2 + 1) * signOr(dy, j % 2 === 0 ? -1 : 1)
        }
        a.x += pushX
        a.y += pushY
        b.x -= pushX
        b.y -= pushY
        iterMoved = true
      }
    }
    if (!iterMoved) break
  }

  return nodes.map((n) => {
    const p = pos.get(n.id)!
    const moved = p.x !== n.position.x || p.y !== n.position.y
    return { id: n.id, position: p, moved }
  })
}

/** Math.sign(0) is 0, which would zero out our push vector when two
 *  nodes are exactly aligned. Fall back to a sign derived from the
 *  pair index so perfectly-stacked nodes still get separated. */
function signOr(value: number, fallback: number): number {
  return Math.sign(value) || fallback
}