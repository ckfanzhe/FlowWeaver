/**
 * Custom node components for all 6 v1 node types.
 * Each maps a `type` string to a React component receiving React Flow's NodeProps.
 *
 * : `parallel` + `steps` collapsed to `flow`
 * — the body renders mode-aware (concurrent fan-out vs ordered
 * pipeline) using `FlowNodeConfig.mode`.
 *
 * : `router` + `condition` collapsed to `branch`
 * — the body renders mode-aware (N-ary `Router` vs binary `Condition`)
 * using `BranchNodeConfig.mode`.
 *
 * : `http` + `mcp` + `tools` collapsed to `tool`
 * — the body dispatches on `cfg.source`.
 *
 * : `human_input` → `ask` (kind=control_flow).
 *
 * : the 5 preset types (wikipedia /
 * tavily_search / duckduckgo / calculator / arxiv_search) collapsed
 * into the unified `tool` node's `preset` config discriminator. The
 * preset badge is rendered INSIDE ToolNode's body so the user can
 * still tell at a glance which toolkit a node represents — no
 * dedicated wrapper component per preset is needed.
 */
import type { NodeProps, NodeTypes } from '@xyflow/react'
import { useWorkflowStore } from '../../store/workflowStore'
import { useSettingsStore } from '../../store/settingsStore'
import { useT } from '../../i18n'
import { BaseNode } from './BaseNode'
import type {
  AgentNodeConfig,
  AskConfig,
  BranchNodeConfig,
  FlowNodeConfig,
  KnowledgeNodeConfig,
  LoopNodeConfig,
  ToolNodeConfig,
} from '../../types/workflow'
import type { BranchMode } from '../../types/workflow'

interface DataBag {
  label: string
  config: Record<string, unknown>
}

function getConfig<T>(data: unknown): T {
  return ((data as unknown as DataBag)?.config ?? {}) as T
}

/**
 * Highlight a node when EITHER React Flow considers it selected (left-click)
 * OR our store's `selectedNodeId` matches (set by right-click, which never
 * triggers React Flow's selection model). Keeping both in sync means the
 * outline lights up the moment the user clicks — whether with left or right.
 */
function useIsSelected(nodeId: string, rfSelected: boolean | undefined): boolean {
  const storeSelected = useWorkflowStore((s) => s.selectedNodeId === nodeId)
  return !!storeSelected || !!rfSelected
}

// ─────────────────────────────────────────────────────────────────
// Agent
// ─────────────────────────────────────────────────────────────────
export function AgentNode({ id, data, selected }: NodeProps) {
  const bag = data as unknown as DataBag
  const cfg = getConfig<AgentNodeConfig>(bag)
  const presets = useSettingsStore((s) => s.presets)
  // Resolve the preset that will actually run for this node. Mirrors the
  // backend's `_agent_handler` priority: explicit presetId → system default
  // preset → bare modelId on legacy inline configs. Falls back to a generic
  // label only when nothing at all is configured.
  const defaultPreset = presets.find((p) => p.isDefault) ?? null
  const selectedPreset = cfg.model?.presetId
    ? presets.find((p) => p.id === cfg.model!.presetId) ?? null
    : null
  const effective = selectedPreset ?? defaultPreset
  // What we print on the canvas: prefer the preset's friendly name (e.g.
  // "Claude Sonnet 4.5"), annotated with ★ when it's the system default so
  // users can tell at a glance which agents are using the global one.
  const display =
    effective
      ? `${effective.name}${effective.isDefault ? ' ★' : ''}`
      : cfg.model?.modelId ?? null
  return (
    <BaseNode type="agent" label={bag?.label} selected={useIsSelected(id, selected)} nodeId={id}>
      <div className="font-mono text-[10px] leading-relaxed">
        {display ? (
          <div className="truncate">{display}</div>
        ) : (
          <div className="opacity-60">no model</div>
        )}
        {cfg.instructions && (
          <div className="line-clamp-2 opacity-70 mt-1">
            {cfg.instructions.slice(0, 60)}
            {cfg.instructions.length > 60 ? '…' : ''}
          </div>
        )}
      </div>
    </BaseNode>
  )
}

// ─────────────────────────────────────────────────────────────────
// Branch — 
//
// Single node type replacing `router` + `condition`. The body
// discriminates by `config.mode`:
//   * `switch`   → "N branch(es)" count badge (the N-ary Router primitive)
//   * `if-else`  → shows the configured evaluator hint + optional elseTarget
//
// Without an entry in `customNodeTypes` below, React Flow falls back
// to its default unstyled rectangle — same "white board" drift that
// bit wikipedia / preset types earlier.
// ─────────────────────────────────────────────────────────────────
export function BranchNode({ id, data, selected }: NodeProps) {
  const bag = data as unknown as DataBag
  const cfg = getConfig<BranchNodeConfig>(bag)
  const mode: BranchMode = cfg.mode ?? 'switch'
  const branches = cfg.branches ?? []
  if (mode === 'switch') {
    return (
      <BaseNode type="branch" label={bag?.label} selected={useIsSelected(id, selected)} nodeId={id}>
        <div className="font-mono text-[10px] leading-relaxed">
          <div className="opacity-70 line-clamp-2">{cfg.selector?.expression || '(no selector)'}</div>
          <div className="mt-1">{branches.length} branch(es)</div>
        </div>
      </BaseNode>
    )
  }
  return (
    <BaseNode type="branch" label={bag?.label} selected={useIsSelected(id, selected)} nodeId={id}>
      <div className="font-mono text-[10px] leading-relaxed">
        <div className="opacity-70 line-clamp-2">{cfg.evaluator?.expression || 'always'}</div>
        {cfg.elseTarget && (
          <div className="mt-1 opacity-60">↳ else: {cfg.elseTarget}</div>
        )}
      </div>
    </BaseNode>
  )
}

// ─────────────────────────────────────────────────────────────────
// Flow — 
//
// Single node type replacing `parallel` + `steps`. The body
// discriminates by `config.mode`:
//   * `parallel`   → "N branch(es)" count badge
//   * `sequential` → "N step(s) · in order" + ordered list of
//                    first 3 targets + optional HITL badge (only
//                    when `requiresConfirmation` is set; ignored
//                    in `parallel` mode).
//
// Without an entry in `customNodeTypes` below, React Flow falls
// back to its default unstyled rectangle — same "white board"
// drift that bit wikipedia / preset types earlier.
// ─────────────────────────────────────────────────────────────────
export function FlowNode({ id, data, selected }: NodeProps) {
  const bag = data as unknown as DataBag
  const cfg = getConfig<FlowNodeConfig>(bag)
  const mode: 'parallel' | 'sequential' = cfg.mode ?? 'parallel'
  const branches = cfg.branches ?? []
  const needsConfirm = !!cfg.requiresConfirmation && mode === 'sequential'
  const t = useT()
  return (
    <BaseNode type="flow" label={bag?.label} selected={useIsSelected(id, selected)} nodeId={id}>
      <div className="font-mono text-[10px] leading-relaxed">
        <div className="opacity-70">
          {mode === 'parallel'
            ? `${branches.length} branch(es)`
            : `${branches.length} step(s) · in order`}
        </div>
        {mode === 'sequential' && branches.length > 0 && (
          <ul className="mt-1 space-y-0.5">
            {branches.slice(0, 3).map((b, i) => (
              <li key={`${b.target}-${i}`} className="line-clamp-1">
                {i + 1}. {b.label || b.target}
              </li>
            ))}
            {branches.length > 3 && <li className="opacity-60">+ {branches.length - 3} more</li>}
          </ul>
        )}
        {needsConfirm && (
          <div className="mt-1 opacity-60">⏸ confirm before first step</div>
        )}
        {mode === 'parallel' && (
          <div className="mt-1 opacity-60">{t('nodes.flow.description')}</div>
        )}
      </div>
    </BaseNode>
  )
}

// ─────────────────────────────────────────────────────────────────
// Loop — iterate a step up to N times
// ─────────────────────────────────────────────────────────────────
export function LoopNode({ id, data, selected }: NodeProps) {
  const bag = data as unknown as DataBag
  const cfg = getConfig<LoopNodeConfig>(bag)
  return (
    <BaseNode type="loop" label={bag?.label} selected={useIsSelected(id, selected)} nodeId={id}>
      <div className="font-mono text-[10px] leading-relaxed">
        <div className="opacity-70">
          max {cfg.maxIterations ?? 3} iteration(s)
        </div>
        {cfg.endCondition && (
          <div className="mt-1 line-clamp-1 opacity-60">
            stop on: <span className="font-mono">{cfg.endCondition}</span>
          </div>
        )}
        {cfg.forwardIterationOutput && (
          <div className="mt-1 opacity-60">↻ feed output forward</div>
        )}
      </div>
    </BaseNode>
  )
}

// ─────────────────────────────────────────────────────────────────
// Ask (renamed from `HumanInputNode`; same shape).
// ─────────────────────────────────────────────────────────────────
export function AskNode({ id, data, selected }: NodeProps) {
  const bag = data as unknown as DataBag
  const cfg = getConfig<AskConfig>(bag)
  return (
    <BaseNode type="ask" label={bag?.label} selected={useIsSelected(id, selected)} nodeId={id}>
      <div className="font-mono text-[10px] leading-relaxed">
        <div className="opacity-70 line-clamp-2">{cfg.prompt || '(no prompt)'}</div>
        <div className="mt-1 opacity-60">{cfg.inputType ?? 'text'}</div>
      </div>
    </BaseNode>
  )
}

// ─────────────────────────────────────────────────────────────────
// Tool — : replaces the prior standalone `http` +
// `mcp` + `tools` nodes. The body dispatches on `cfg.source` to one of
// 3 render shapes (HTTP request / MCP server picker / function list).
// Carries over the bodies of the deleted `HttpNode` / `McpNode` /
// `ToolsNode` verbatim — only the discriminator wrapper is new.
//
// The 5 preset tool types (wikipedia /
// tavily_search / duckduckgo / calculator / arxiv_search) collapsed
// into this same body via the `cfg.preset` discriminator. When
// `cfg.preset` is set, the body renders a preset badge at the top
// (so the user can spot which toolkit the canvas node represents)
// and then falls through to the underlying source render shape.
// ─────────────────────────────────────────────────────────────────
// Tool-source node: only has a RIGHT-side handle (source) so the user
// can draw a `tool_attachment` edge INTO an agent. The left-side target
// handle is suppressed because `tool` nodes are not part of the
// workflow's dataflow topology — they're definitions that get attached
// to an agent.
export function ToolNode({ id, data, selected }: NodeProps) {
  const bag = data as unknown as DataBag
  const cfg = getConfig<ToolNodeConfig>(bag)
  const source: ToolNodeConfig['source'] = cfg.source ?? 'function'
  const preset = cfg.preset ?? null
  const t = useT()
  return (
    <BaseNode type="tool" label={bag?.label} selected={useIsSelected(id, selected)} nodeId={id} hasInput={false}>
      <div className="font-mono text-[10px] leading-relaxed">
        {preset && (
          // : preset badge — surfaces which
          // toolkit preset this `tool` node represents (wikipedia /
          // tavily_search / duckduckgo / calculator / arxiv_search).
          // The badge is the only visual differentiator between
          // otherwise-identical tool bodies on the canvas.
          <div className="opacity-70">
            {t('panel.tool.presetBadge', { name: preset })}
          </div>
        )}
        {source === 'mcp' && (
          <>
            <div className="opacity-70 line-clamp-2">
              {cfg.serverId ? `server: ${cfg.serverId}` : t('nodes.mcp.description')}
            </div>
            {cfg.toolNamePrefix && (
              <div className="mt-1 opacity-60">prefix: {cfg.toolNamePrefix}</div>
            )}
          </>
        )}
        {source === 'http' && (
          <>
            <div className="opacity-70">
              <span className="font-semibold">{cfg.method ?? 'GET'}</span>{' '}
              <span className="line-clamp-1">{cfg.baseUrl || '(no base URL)'}{cfg.path ?? ''}</span>
            </div>
            <div className="mt-1 line-clamp-1 opacity-60">→ {cfg.toolName || '(tool name)'}</div>
          </>
        )}
        {source === 'function' && (() => {
          const fns = cfg.functions ?? []
          if (fns.length === 0) {
            return <div className="opacity-60">no functions yet</div>
          }
          return (
            <>
              <div className="opacity-70">{fns.length} function(s)</div>
              <ul className="mt-1 space-y-0.5">
                {fns.slice(0, 3).map((f) => (
                  <li key={f.name} className="line-clamp-1">· {f.name}()</li>
                ))}
                {fns.length > 3 && <li className="opacity-60">+ {fns.length - 3} more</li>}
              </ul>
            </>
          )
        })()}
      </div>
    </BaseNode>
  )
}

// ─────────────────────────────────────────────────────────────────
// The previous WikipediaNode + PresetToolkitNode wrappers are
// deleted. The 5 preset types (wikipedia / tavily_search /
// duckduckgo / calculator / arxiv_search) collapsed into the unified
// `tool` node's `preset` config discriminator — they no longer have
// their own NodeType literal and never reach this registry. Legacy
// envelopes carrying `type: "wikipedia"` (etc.) are migrated to
// `type: "tool"` + `config.preset = "<name>"` by `_compat.migrate_envelope`
// on read, so by the time React Flow looks up customNodeTypes[type]
// the type is always a 6-member base type.
//
// The ToolNode body shows a preset badge when `cfg.preset` is set
// (so the user still sees which toolkit the canvas node represents),
// but a dedicated component per preset is no longer needed.
// ─────────────────────────────────────────────────────────────────

// ─────────────────────────────────────────────────────────────────
// Knowledge — : RAG / vector DB source.
// Parallel to `tool`/`tool_source` architecturally. `hasInput={false}`
// (no dataflow input handle) because knowledge nodes are wired via
// `knowledge_attachment` edges only — they're not in the workflow's
// dataflow topology.
//
// The body renders three summary badges so the user can tell at a
// glance what's configured:
//   1. Vector DB kind + table name (e.g. "pgvector · agno_kb")
//   2. Embedder kind + model id (e.g. "openai · text-embedding-3-small")
//   3. Source count badge (e.g. "3 source(s)" — paths / URLs / text)
// `addKnowledgeToContext` is surfaced as a small "→ auto-inject" hint
// when true, so the user knows the agent will see retrieved chunks
// without explicitly calling search_knowledge.
//
// v1 ships a single hard-coded stack (locked 2026-08-25): `pgvector`
// + OpenAI embedder. The `vectorDb` / `embedder` discriminators still
// come from the store (forward-compat) but the body reads the pgvector
// + openai fields directly — no per-backend dispatch table.
// ─────────────────────────────────────────────────────────────────
export function KnowledgeNode({ id, data, selected }: NodeProps) {
  const bag = data as unknown as DataBag
  const cfg = getConfig<KnowledgeNodeConfig>(bag)
  const vectorDb = cfg.vectorDb ?? 'pgvector'
  const embedder = cfg.embedder ?? 'openai'
  const sources = cfg.sources ?? []
  // Surface the table name so the user can spot which table is being
  // used without opening the property panel. v1 only ever renders the
  // pgvector branch.
  const tableLabel = cfg.pgvectorTableName || 'agno_kb'
  // Same for embedder model id — pick the field that matches the
  // chosen embedder so the user sees what they configured.
  const embedderLabel = cfg.openaiModel || 'text-embedding-3-small'
  return (
    <BaseNode type="knowledge" label={bag?.label} selected={useIsSelected(id, selected)} nodeId={id} hasInput={false}>
      <div className="font-mono text-[10px] leading-relaxed">
        <div className="opacity-70">
          <span className="font-semibold">{vectorDb}</span>
          {tableLabel ? <span className="opacity-60"> · {tableLabel}</span> : null}
        </div>
        <div className="opacity-70 mt-0.5">
          <span className="font-semibold">{embedder}</span>
          {embedderLabel ? <span className="opacity-60"> · {embedderLabel}</span> : null}
        </div>
        <div className="mt-1 opacity-60">
          {sources.length === 0
            ? 'no sources yet'
            : `${sources.length} source${sources.length === 1 ? '' : 's'}`}
        </div>
        {cfg.addKnowledgeToContext && (
          <div className="mt-1 opacity-60">→ auto-inject</div>
        )}
      </div>
    </BaseNode>
  )
}

// Registry — map node type -> component for React Flow.
// React Flow renders a plain unstyled rectangle for any node type
// absent from this map. After the node-type collapse: 14 → 6
// base types — the 5 preset types are gone from this registry.
export const customNodeTypes: NodeTypes = {
  agent: AgentNode,
  // : `router` + `condition` collapsed to `branch`
  // — mode discriminator lives in `config.mode`.
  branch: BranchNode,
  // : `parallel` + `steps` collapsed to `flow`.
  flow: FlowNode,
  loop: LoopNode,
  // : `human_input` → `ask`.
  ask: AskNode,
  // : `http` + `mcp` + `tools` collapsed to `tool`
  // — source discriminator (`mcp` | `http` | `function`) lives in
  // `config.source`. The body's render dispatches on this.
  // : the same ToolNode body renders for the
  // 5 collapsed preset types via the `preset` config discriminator.
  tool: ToolNode,
  // : RAG / knowledge source.
  // `hasInput={false}` — knowledge nodes are NOT in the workflow's
  // dataflow topology; they're source nodes attached to agents via
  // `knowledge_attachment` edges. Same handle pattern as `ToolNode`.
  // The body shows: vector DB kind + table name, embedder kind +
  // model id, source count badge.
  knowledge: KnowledgeNode,
}