/**
 * Manifest-walk helpers — single source of truth for `extends` chain
 * traversal.
 *
 * phase . Before this module the chain walk lived in
 * two places: `components/Nodes/nodeStyles.ts:resolveVisual` and
 * `components/PropertyPanel/forms/registry.ts:resolveForm`. Both
 * used the same depth-first algorithm with the same depth bound but
 * were copy-pasted, so any future change (cycle detection logging,
 * error reporting, …) had to be made twice. Centralising here also
 * lets the upcoming 3-level-extends regression test pin one
 * implementation instead of two.
 */
import type { NodeTypesManifest } from '../api/nodeTypes'

/** Default depth bound for `extends` walks. Backs the backend's
 *  cycle check at startup — the frontend can't depend on that, so
 *  we cap the walk here as well. */
export const MAX_EXTENDS_DEPTH = 8

/**
 * Walk the `extends` chain starting at `start`, calling `visit` on
 * each ancestor (the start itself is NOT visited — `visit` is for
 * the lookup table, not the start node).
 *
 * Returns the first non-null value `visit` produces, or `null` if
 * every ancestor returned null (or the chain was exhausted before
 * depth `MAX_EXTENDS_DEPTH`).
 *
 * The walk is depth-first: `a extends b extends c` visits in the
 * order `a → b → c`. The caller decides what "first hit" means —
 * `resolveForm` uses it for "first ancestor with a form component",
 * `resolveVisual` uses it for "first ancestor with a visual".
 *
 * The walk terminates early when `visit` returns non-null — callers
 * rely on this to short-circuit once they've found their target.
 */
export function walkExtends<T>(
  start: string,
  manifest: NodeTypesManifest,
  visit: (name: string) => T | null,
  maxDepth: number = MAX_EXTENDS_DEPTH,
): T | null {
  const entries = manifest.entries
  let parentName: string | null | undefined = entries[start]?.extends
  let depth = 0
  while (parentName && depth < maxDepth) {
    const hit = visit(parentName)
    if (hit !== null && hit !== undefined) return hit
    parentName = entries[parentName]?.extends
    depth += 1
  }
  return null
}