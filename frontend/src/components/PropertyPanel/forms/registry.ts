/**
 * Form registry — phase of the node-system refactor .
 *
 * Maps node types to the React component that renders their property
 * panel form. The lookup walks `extends` chains so any future
 * `extends: "tool"` preset automatically inherits the parent's
 * form without an entry in this file.
 *
 * : the 5 preset types (wikipedia /
 * tavily_search / duckduckgo / calculator / arxiv_search) collapsed
 * into the `tool` node's `preset` config discriminator. The
 * `ToolPresetForm` sub-form renders INSIDE `ToolForm` when
 * `cfg.preset` is set — it's no longer a top-level registry entry.
 * The four toolkit-preset rows are gone.
 *
 * Adding a NEW node type:
 *
 *   1. Drop a `<NewType>Form.tsx` next to its siblings.
 *   2. Add it to `FORM_REGISTRY` below.
 *   3. Done. No edits to PropertyPanel.tsx.
 *
 * Adding a NEW preset:
 *
 *   1. Add the manifest row with `extends: "parent"` + `ui.form` if
 *      the parent isn't already mapped.
 *   2. Done. No edits here — `extends` resolution finds the parent's
 *      form for you.
 *
 * Why a registry (and not a switch in PropertyPanel.tsx)?
 *
 *   * Presets can extend any type, and the chain depth is dynamic.
 *     A switch only handles one level; `resolveForm` walks the
 *     chain until it finds a registered component.
 *   * Tests can exercise the registry directly without rendering a
 *     full React tree — see `forms/__tests__/registry.test.ts`.
 */
import type { NodeType } from '../../../types/workflow'
import type { NodeTypesManifest as Manifest } from '../../../api/nodeTypes'
import { walkExtends } from '../../../lib/manifest'

import { AgentForm } from '../AgentForm'
import { BranchForm } from '../BranchForm'
import { FlowForm } from '../FlowForm'
import { LoopForm } from '../LoopForm'
// `HumanInputForm` renamed to `AskForm`.
import { AskForm } from '../AskForm'
import { ToolForm } from '../ToolForm'

/** Form-component contract — every entry satisfies this. */
export type FormComponent = React.FC<{ nodeId: string }>

/**
 * Per-type form lookup. Keys are node types, values are the React
 * component that renders the property panel for that type.
 *
 * After the node-type collapse: 14 → 6 base types. The 5
 * preset tool types (wikipedia / tavily_search / duckduckgo /
 * calculator / arxiv_search) collapsed into the `tool` node's
 * `preset` config discriminator and are NOT registered here — they
 * fall through to `ToolForm` via `resolveForm`'s `extends` walk
 * (see the manifest's `extends: "tool"` rows; new presets add
 * their own manifest row + extend).
 */
export const FORM_REGISTRY: Partial<Record<NodeType, FormComponent>> = {
  agent: AgentForm,
  // : `router` + `condition` collapsed to `branch`
  // — top-level `mode` discriminator (`switch` | `if-else`) inside the
  // form. Switch mode keeps the prior RouterForm's 3-mode selector
  // editor; if-else mode keeps the prior ConditionForm's 3-mode
  // evaluator + HITL subform.
  branch: BranchForm,
  // : `parallel` + `steps` collapsed to `flow`
  // — mode discriminator inside the form.
  flow: FlowForm,
  loop: LoopForm,
  // : `human_input` → `ask`.
  ask: AskForm,
  // : `http` + `mcp` + `tools` collapse to a
  // single `tool` form. The form switches its sub-form on
  // `cfg.source` (`'http'` | `'mcp'` | `'function'`).
  // : preset toolkits + wikipedia route
  // through here via `cfg.preset` — `ToolPresetForm` renders INSIDE
  // the same `ToolForm` panel when preset is set, so no top-level
  // registry entry is needed.
  tool: ToolForm,
}

/**
 * Resolve a node type to its form component.
 *
 * Walks the `extends` chain in the manifest so preset types
 * automatically inherit their parent's form. The chain is bounded
 * by depth to defend against accidental cycles in the manifest
 * (the backend already rejects cycles at load time, but the
 * frontend shouldn't depend on that — it must remain usable when
 * the manifest fetch fails and only the fallback manifest is
 * loaded).
 *
 * @returns the form component, or `null` if the type doesn't
 *          resolve (no registered form and no `extends` chain).
 */
export function resolveForm(
  type: NodeType,
  manifest: Manifest | null,
  maxDepth = 8,
): FormComponent | null {
  // 1. Direct hit — most common path (and the only path for the
  //    9 base types).
  const direct = FORM_REGISTRY[type]
  if (direct) return direct

  // 2. Walk `extends` chain. phase : the walk moved
  //    to `lib/manifest.walkExtends` so `resolveForm` and
  //    `resolveVisual` share one implementation. The backend's
  //    cycle check at startup means `walkExtends` will never see a
  //    loop, but the depth bound is still applied here as a
  //    safety net for when the manifest fetch fails and the
  //    frontend falls back to the static fallback manifest.
  if (!manifest) return null
  return walkExtends(
    type,
    manifest,
    (name) => FORM_REGISTRY[name as NodeType] ?? null,
    maxDepth,
  )
}