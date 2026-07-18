/**
 * Per-node connection rules — frontend validator (logic layer).
 *
 * row E : the rules table is now generated from
 * the shared JSON (`connectionRules.generated.ts`). This module
 * keeps only the runtime concerns: per-edge-kind dispatch, drag-time
 * validation, and the structured `ConnectionError` shape. Drift
 * between the TS and Python rules tables is no longer possible
 * because both are emitted from the same JSON via a single
 * codegen pass — see `scripts/generate_connection_rules_ts.py`.
 *
 * : edges carry a `kind` field. `dataflow`
 * is the default kind (covered by `CONNECTION_RULES`); the new
 * `tool_attachment` kind lets tool-source nodes (`tools` / `http`
 * / `mcp` / presets) hand their definition to an Agent via a typed
 * edge that does NOT enter the workflow topology. The validator
 * dispatches per-kind; `dataflow` is always checked (even with
 * zero dataflow edges — degree bounds must still fire), and any
 * additional `edge_kinds.<kind>` table is consulted alongside it.
 *
 * Drift is checked by `scripts/check_connection_rules_consistency.py`
 * (runs in CI alongside the backend tests).
 */
import type { NodeType, WorkflowEdge, WorkflowNode } from '../types/workflow';
import {
  CONNECTION_RULES,
  TOOL_ATTACHMENT_RULES,
  TOOL_SOURCE_TYPES,
  type ConnectionRule,
} from './connectionRules.generated';

export type EdgeKind = 'dataflow' | 'tool_attachment';

export const ENTRY_TYPE: NodeType = 'agent'; // workflow's first step is treated as the entry; no dedicated node
export const TERMINAL_TYPE: NodeType = 'agent'; // workflow's last step is the output; no dedicated node

/** Re-exported for callers that previously imported these from here. */
export { CONNECTION_RULES, TOOL_ATTACHMENT_RULES, TOOL_SOURCE_TYPES };
export type { ConnectionRule };

/** Normalise an edge's `kind` to a known value (unknown → "dataflow"). */
export function edgeKindOf(edge: { kind?: string | null }): EdgeKind {
  const k = (edge?.kind ?? '').toString();
  return k === 'tool_attachment' ? 'tool_attachment' : 'dataflow';
}

export type ErrorCode =
  | 'selfLoop'
  | 'duplicateEdge'
  | 'incompatibleSource'
  | 'incompatibleTarget'
  | 'tooManyOutgoing'
  | 'tooManyIncoming'
  | 'missingOutgoing'
  | 'missingIncoming'
  | 'noThen'
  | 'loopBodyViaEdge';

export interface ConnectionError {
  code: ErrorCode;
  node_id?: string;
  edge_id?: string;
  source_id?: string;
  target_id?: string;
  message: string;
}

type RuleTable = Readonly<Record<NodeType, ConnectionRule>>;

function typeOf(node: WorkflowNode): NodeType {
  return node.type as NodeType;
}

function configOf(node: WorkflowNode): Record<string, unknown> {
  return ((node.data?.config as Record<string, unknown> | undefined) ?? {});
}

function ruleOf(
  byId: Map<string, WorkflowNode>,
  id: string,
  table: RuleTable,
): ConnectionRule | null {
  const n = byId.get(id);
  return n ? table[typeOf(n)] ?? null : null;
}

/** Pick the right rule table for an edge's kind. */
function rulesForKind(kind: EdgeKind): RuleTable {
  return kind === 'tool_attachment' ? TOOL_ATTACHMENT_RULES : CONNECTION_RULES;
}

/** Pure validator — mirrors `validate_connections` in the Python module.
 *
 * : per-edge-kind dispatch. `dataflow` edges use
 * the top-level `CONNECTION_RULES` table; `tool_attachment` edges use
 * `TOOL_ATTACHMENT_RULES`. The `dataflow` degree-bound checks always
 * run (even with zero dataflow edges) so `noThen` / `missingOutgoing`
 * fire for incomplete conditions / loops in mixed graphs. Other kinds
 * fire alongside it.
 */
export function validateConnections(
  nodes: readonly WorkflowNode[],
  edges: readonly WorkflowEdge[],
): ConnectionError[] {
  const errors: ConnectionError[] = [];

  const byId = new Map<string, WorkflowNode>();
  for (const n of nodes) {
    if (n?.id) byId.set(n.id, n);
  }

  // Surface globally unknown node types once, before per-kind
  // validation, so the message names the type. Mirrors the backend's
  // behaviour.
  const knownTypes = new Set<string>([
    ...Object.keys(CONNECTION_RULES),
    ...Object.keys(TOOL_ATTACHMENT_RULES),
  ]);
  for (const n of nodes) {
    if (n?.type && !knownTypes.has(n.type)) {
      errors.push({
        code: 'incompatibleSource',
        node_id: n.id,
        message: `unknown node type: ${n.type}`,
      });
    }
  }

  // Per-edge-kind dispatch — group edges by kind and run each kind's
  // validator. `dataflow` is the always-on kind; other kinds join the
  // run when they appear.
  const byKind = new Map<EdgeKind, WorkflowEdge[]>();
  for (const e of edges) {
    const k = edgeKindOf(e);
    const list = byKind.get(k);
    if (list) list.push(e);
    else byKind.set(k, [e]);
  }
  // Always validate dataflow, even when no dataflow edges exist, so
  // degree bounds (`noThen` / `missingOutgoing`) fire for incomplete
  // graphs that contain only tool_attachment edges.
  if (!byKind.has('dataflow')) byKind.set('dataflow', []);

  for (const [kind, kindEdges] of byKind) {
    validateKind(kind, nodes, kindEdges, byId, rulesForKind(kind), errors);
  }

  // ── Loop-body via edge. Only dataflow edges participate in
  // topology; tool_attachment never creates a loop body.
  const dataflowEdges = byKind.get('dataflow') ?? [];
  const outgoing = new Map<string, string[]>();
  for (const n of nodes) outgoing.set(n.id, []);
  for (const e of dataflowEdges) {
    if (outgoing.has(e.source)) outgoing.get(e.source)!.push(e.target);
  }
  for (const n of nodes) {
    if (typeOf(n) !== 'loop') continue;
    const body = configOf(n).bodyTarget as string | undefined;
    if (!body) continue;
    if ((outgoing.get(n.id) ?? []).includes(body)) {
      errors.push({
        code: 'loopBodyViaEdge',
        node_id: n.id,
        source_id: n.id,
        target_id: body,
        message:
          `loop ${n.id} has bodyTarget=${body} but also an outgoing edge ` +
          `to it; the body would execute twice. Remove the edge or clear bodyTarget.`,
      });
    }
  }

  return errors;
}

/** Per-kind validator. Mirrors `check_node_view` on the Python side. */
function validateKind(
  kind: EdgeKind,
  nodes: readonly WorkflowNode[],
  edges: readonly WorkflowEdge[],
  byId: Map<string, WorkflowNode>,
  rules: RuleTable,
  errors: ConnectionError[],
): void {
  const ruleOfFor = (id: string): ConnectionRule | null => ruleOf(byId, id, rules);

  // ── Edge-level checks.
  const seenPairs = new Set<string>();
  for (const e of edges) {
    const src = e.source ?? '';
    const tgt = e.target ?? '';
    const eid = e.id ?? '';

    if (src === tgt) {
      errors.push({
        code: 'selfLoop',
        edge_id: eid,
        source_id: src,
        target_id: tgt,
        message: `edge ${eid || '(no id)'}: source and target are the same node`,
      });
      continue;
    }

    const key = `${src}|${tgt}`;
    if (seenPairs.has(key)) {
      errors.push({
        code: 'duplicateEdge',
        edge_id: eid,
        source_id: src,
        target_id: tgt,
        message: `edge ${eid || '(no id)'}: duplicate of an existing edge`,
      });
      continue;
    }
    seenPairs.add(key);

    const srcRule = ruleOfFor(src);
    const tgtRule = ruleOfFor(tgt);
    if (!srcRule) continue; // dangling source — backend will surface it
    if (!tgtRule) continue;

    // Tool-attachment source has no outgoing edges at all under
    // `CONNECTION_RULES` — its tool-attachment rules differ. If the
    // source's type isn't in the kind's table, the kind doesn't
    // accept it. Skipping here keeps the message targeted.
    if (srcRule.allowed_target_types.size === 0) {
      errors.push({
        code: 'incompatibleSource',
        edge_id: eid,
        source_id: src,
        node_id: src,
        message:
          `node ${src} (${typeOf(byId.get(src)!)}) ` +
          `cannot be the source of a ${kind} edge`,
      });
      continue;
    }

    if (!srcRule.allowed_target_types.has(typeOf(byId.get(tgt)!))) {
      errors.push({
        code: 'incompatibleSource',
        edge_id: eid,
        source_id: src,
        target_id: tgt,
        node_id: src,
        message:
          `node ${src} (${typeOf(byId.get(src)!)}) cannot connect to ` +
          `${tgt} (${typeOf(byId.get(tgt)!)}) via a ${kind} edge`,
      });
      continue;
    }

    if (tgtRule.allowed_source_types.size === 0) {
      errors.push({
        code: 'incompatibleTarget',
        edge_id: eid,
        target_id: tgt,
        node_id: tgt,
        message: `node ${tgt} (${typeOf(byId.get(tgt)!)}) cannot be the target of a ${kind} edge`,
      });
    }
  }

  // ── Node-level degree checks.
  const outgoing = new Map<string, string[]>();
  const incoming = new Map<string, string[]>();
  for (const n of nodes) {
    outgoing.set(n.id, []);
    incoming.set(n.id, []);
  }
  for (const e of edges) {
    if (outgoing.has(e.source)) outgoing.get(e.source)!.push(e.target);
    if (incoming.has(e.target)) incoming.get(e.target)!.push(e.source);
  }

  for (const n of nodes) {
    const t = typeOf(n);
    const rule = rules[t];
    if (!rule) continue;
    const outDeg = outgoing.get(n.id)?.length ?? 0;
    const inDeg = incoming.get(n.id)?.length ?? 0;

    if (rule.max_outgoing !== null && outDeg > rule.max_outgoing) {
      errors.push({
        code: 'tooManyOutgoing',
        node_id: n.id,
        message:
          `node ${n.id} (${t}) has ${outDeg} ${kind} outgoing edges; ` +
          `max is ${rule.max_outgoing}`,
      });
    }
    if (rule.min_outgoing > outDeg) {
      // : `condition` collapsed to
      // `branch` with `mode='if-else'`. The `noThen` code still
      // applies when the if-else branch has no `then` target; we
      // distinguish it from generic `missingOutgoing` by checking
      // the config's `mode` field. (Branch's connection-layer
      // rule has `min_outgoing=0` per design — the strict
      // `min_outgoing=1, max_outgoing=2` if-else check lives at
      // the strategy layer in `BranchStrategy._build_if_else`.
      // This code path is reached only if a future connection
      // rule tightens branch.)
      const cfg = configOf(n);
      const isIfElse = t === 'branch' && cfg.mode === 'if-else';
      errors.push({
        code: isIfElse ? 'noThen' : 'missingOutgoing',
        node_id: n.id,
        message:
          `node ${n.id} (${t}) has ${outDeg} ${kind} outgoing edges; ` +
          `min is ${rule.min_outgoing}`,
      });
    }
    if (rule.max_incoming !== null && inDeg > rule.max_incoming) {
      errors.push({
        code: 'tooManyIncoming',
        node_id: n.id,
        message:
          `node ${n.id} (${t}) has ${inDeg} ${kind} incoming edges; ` +
          `max is ${rule.max_incoming}`,
      });
    }
    if (rule.min_incoming > inDeg) {
      errors.push({
        code: 'missingIncoming',
        node_id: n.id,
        message:
          `node ${n.id} (${t}) has ${inDeg} ${kind} incoming edges; ` +
          `min is ${rule.min_incoming}`,
      });
    }
  }
}

/**
 * Check whether adding the candidate edge `source → target` to the
 * graph would violate any rule **that is caused by the candidate**.
 *
 * The full `validateConnections` surfaces workflow-level problems
 * like `missingOutgoing` / `loopBodyViaEdge` that EVERY candidate edge
 * "fails" because the graph is incomplete during a drag. That's not
 * useful for the canvas's "which targets are reachable?" view — every
 * node would dim even when the candidate itself is fine.
 *
 * This function only checks rules that the candidate could cause:
 *   * `selfLoop`           — source == target
 *   * `duplicateEdge`      — that exact (src, tgt) pair already exists
 *   * `incompatibleSource` — source's type can't have outgoing edges
 *   * `incompatibleTarget` — target's type can't be wired to
 *   * `tooManyOutgoing`    — would push source over its max outgoing
 *   * `tooManyIncoming`    — would push target over its max incoming
 *
 * : also accepts an optional `kind` argument
 * (default `dataflow`) so the canvas can validate a candidate
 * tool_attachment drag against `TOOL_ATTACHMENT_RULES` instead of the
 * dataflow table — that's how a `tools` node knows its drag to an
 * `agent` is legal at all.
 *
 * Workflow-level checks (`loopBodyViaEdge`) are deliberately excluded.
 * Returns an empty list if the candidate is legal.
 */
export function wouldBeValidConnection(
  sourceId: string,
  targetId: string,
  nodes: readonly WorkflowNode[],
  edges: readonly WorkflowEdge[],
  kind: EdgeKind = 'dataflow',
): ConnectionError[] {
  const errors: ConnectionError[] = [];

  if (!sourceId || !targetId) return errors;
  if (sourceId === targetId) {
    errors.push({
      code: 'selfLoop',
      source_id: sourceId,
      target_id: targetId,
      message: `node ${sourceId} cannot connect to itself`,
    });
    return errors;
  }

  const byId = new Map<string, WorkflowNode>();
  for (const n of nodes) if (n?.id) byId.set(n.id, n);
  const srcNode = byId.get(sourceId);
  const tgtNode = byId.get(targetId);
  if (!srcNode || !tgtNode) {
    errors.push({
      code: srcNode ? 'incompatibleTarget' : 'incompatibleSource',
      source_id: sourceId,
      target_id: targetId,
      message: srcNode
        ? `node ${targetId} not found`
        : `node ${sourceId} not found`,
    });
    return errors;
  }

  const srcType = typeOf(srcNode);
  const tgtType = typeOf(tgtNode);
  const rules = rulesForKind(kind);
  const srcRule = rules[srcType];
  const tgtRule = rules[tgtType];
  if (!srcRule || !tgtRule) {
    errors.push({
      code: srcRule ? 'incompatibleTarget' : 'incompatibleSource',
      source_id: sourceId,
      target_id: targetId,
      message: srcRule
        ? `node ${targetId} has unknown type ${tgtType}`
        : `node ${sourceId} has unknown type ${srcType}`,
    });
    return errors;
  }

  // 3a. Source has no outgoing edges at all under this kind's table?
  if (srcRule.allowed_target_types.size === 0) {
    errors.push({
      code: 'incompatibleSource',
      source_id: sourceId,
      target_id: targetId,
      node_id: sourceId,
      message:
        `node ${sourceId} (${srcType}) cannot be the source of a ${kind} edge`,
    });
    return errors;
  }

  // 3b. Target's type refuses to be wired under this kind's table?
  if (tgtRule.allowed_source_types.size === 0) {
    errors.push({
      code: 'incompatibleTarget',
      source_id: sourceId,
      target_id: targetId,
      node_id: targetId,
      message: `node ${targetId} (${tgtType}) cannot be the target of a ${kind} edge`,
    });
    return errors;
  }

  // 3c. Target's type not in source's allowed targets?
  if (!srcRule.allowed_target_types.has(tgtType)) {
    errors.push({
      code: 'incompatibleSource',
      source_id: sourceId,
      target_id: targetId,
      node_id: sourceId,
      message:
        `node ${sourceId} (${srcType}) cannot connect to ` +
        `${targetId} (${tgtType}) via a ${kind} edge`,
    });
    return errors;
  }

  // 4. Duplicate edge — only same-kind duplicates count; the same
  // (src, tgt) pair under a different kind is legal.
  for (const e of edges) {
    if (
      e.source === sourceId &&
      e.target === targetId &&
      edgeKindOf(e) === kind
    ) {
      errors.push({
        code: 'duplicateEdge',
        edge_id: e.id,
        source_id: sourceId,
        target_id: targetId,
        message: `edge ${e.id || '(no id)'}: duplicate of an existing ${kind} edge`,
      });
      return errors;
    }
  }

  // 5. Degree counts: would adding this edge push source/target over max?
  // Only count same-kind edges toward the degree.
  let srcOut = 0;
  let tgtIn = 0;
  for (const e of edges) {
    if (edgeKindOf(e) !== kind) continue;
    if (e.source === sourceId) srcOut++;
    if (e.target === targetId) tgtIn++;
  }
  if (srcRule.max_outgoing !== null && srcOut + 1 > srcRule.max_outgoing) {
    errors.push({
      code: 'tooManyOutgoing',
      node_id: sourceId,
      message:
        `node ${sourceId} (${srcType}) has ${srcOut} ${kind} outgoing edges; ` +
        `max is ${srcRule.max_outgoing}`,
    });
  }
  if (tgtRule.max_incoming !== null && tgtIn + 1 > tgtRule.max_incoming) {
    errors.push({
      code: 'tooManyIncoming',
      node_id: targetId,
      message:
        `node ${targetId} (${tgtType}) has ${tgtIn} ${kind} incoming edges; ` +
        `max is ${tgtRule.max_incoming}`,
    });
  }

  return errors;
}