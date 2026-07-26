/**
 * GENERATED FILE — DO NOT EDIT.
 *
 * Source of truth: shared/nodes.manifest.json.
 * Regenerate with:  python scripts/generate_node_types.py
 * CI check:         python scripts/check_node_types_consistency.py
 *
 * Phase 9 (2026-08) of the node-system refactor. Adding a new
 * preset in the manifest (one `extends:` block) automatically
 * extends this union — typecheck catches drift between Python
 * and TypeScript without anyone having to remember to hand-edit
 * two lists.
 *
 * `workflow.ts` re-exports this type as `NodeType` so existing
 * imports (`import type { NodeType } from './workflow'`) keep
 * working unchanged.
 */

export type GeneratedNodeType =
    'agent'
  | 'ask'
  | 'branch'
  | 'flow'
  | 'knowledge'
  | 'loop'
  | 'tool'
;
