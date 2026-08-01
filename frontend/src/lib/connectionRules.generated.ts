/**
 * GENERATED FILE — DO NOT EDIT.
 *
 * Source of truth: shared/connection_rules.json.
 * Regenerate with:  python scripts/generate_connection_rules_ts.py
 * CI check:         python scripts/check_connection_rules_consistency.py
 *
 * The rules table used to be loaded by both the Python
 * backend (connection_rules.py) and the TS frontend
 * (connectionValidation.ts), each expanding @group refs
 * independently. Drift was possible. This codegen pass
 * runs once, expanding the JSON's @group refs and emitting a
 * frozen TS module — the runtime imports nothing from the JSON.
 */
import type { NodeType } from '../types/workflow';

export const GROUPS: Readonly<Record<string, ReadonlyArray<NodeType>>> = {
  "control_flow": ["ask"],
  "executable": ["agent", "ask", "branch", "flow", "loop"],
  "knowledge_source": ["knowledge"],
  "tool_source": ["tool"],
};

export const EXECUTABLE_TYPES: ReadonlySet<NodeType> = new Set<NodeType>(["agent", "ask", "branch", "flow", "loop"]);
export const TOOL_SOURCE_TYPES: ReadonlySet<NodeType> = new Set<NodeType>(["tool"]);
export const KNOWLEDGE_SOURCE_TYPES: ReadonlySet<NodeType> = new Set<NodeType>(["knowledge"]);

export interface ConnectionRule {
  /** Who is allowed to have an outgoing edge INTO this node. */
  allowed_source_types: ReadonlySet<NodeType>;
  /** Which targets this node may connect to via outgoing edges. */
  allowed_target_types: ReadonlySet<NodeType>;
  max_outgoing: number | null;
  min_outgoing: number;
  min_incoming: number;
  max_incoming: number | null;
}

/** Dataflow rule table — top-level `rules` block in the JSON. Default kind. */
export const CONNECTION_RULES: Readonly<Record<NodeType, ConnectionRule>> = {
  "agent": {
    allowed_source_types: new Set<NodeType>(["agent", "ask", "branch", "flow", "loop"]),
    allowed_target_types: new Set<NodeType>(["agent", "ask", "branch", "flow", "loop"]),
    max_outgoing: 1,
    min_outgoing: 0,
    min_incoming: 0,
    max_incoming: null,
},
  "ask": {
    allowed_source_types: new Set<NodeType>(["agent", "ask", "branch", "flow", "loop"]),
    allowed_target_types: new Set<NodeType>(["agent", "ask", "branch", "flow", "loop"]),
    max_outgoing: 1,
    min_outgoing: 0,
    min_incoming: 0,
    max_incoming: null,
},
  "branch": {
    allowed_source_types: new Set<NodeType>(["agent", "ask", "branch", "flow", "loop"]),
    allowed_target_types: new Set<NodeType>(["agent", "ask", "branch", "flow", "loop"]),
    max_outgoing: null,
    min_outgoing: 0,
    min_incoming: 0,
    max_incoming: null,
},
  "flow": {
    allowed_source_types: new Set<NodeType>(["agent", "ask", "branch", "flow", "loop"]),
    allowed_target_types: new Set<NodeType>(["agent", "ask", "branch", "flow", "loop"]),
    max_outgoing: null,
    min_outgoing: 0,
    min_incoming: 0,
    max_incoming: null,
},
  "knowledge": {
    allowed_source_types: new Set<NodeType>([]),
    allowed_target_types: new Set<NodeType>([]),
    max_outgoing: 0,
    min_outgoing: 0,
    min_incoming: 0,
    max_incoming: 0,
},
  "loop": {
    allowed_source_types: new Set<NodeType>(["agent", "ask", "branch", "flow", "loop"]),
    allowed_target_types: new Set<NodeType>(["agent", "ask", "branch", "flow", "loop"]),
    max_outgoing: 1,
    min_outgoing: 0,
    min_incoming: 0,
    max_incoming: null,
},
  "tool": {
    allowed_source_types: new Set<NodeType>([]),
    allowed_target_types: new Set<NodeType>([]),
    max_outgoing: 0,
    min_outgoing: 0,
    min_incoming: 0,
    max_incoming: 0,
},
};

/** knowledge_attachment rule table — `edge_kinds.knowledge_attachment.rules` in the JSON. */
export const KNOWLEDGE_ATTACHMENT_RULES: Readonly<Record<NodeType, ConnectionRule>> = {
  "agent": {
    allowed_source_types: new Set<NodeType>(["knowledge"]),
    allowed_target_types: new Set<NodeType>([]),
    max_outgoing: 0,
    min_outgoing: 0,
    min_incoming: 0,
    max_incoming: 1,
},
  "knowledge": {
    allowed_source_types: new Set<NodeType>([]),
    allowed_target_types: new Set<NodeType>(["agent"]),
    max_outgoing: 1,
    min_outgoing: 0,
    min_incoming: 0,
    max_incoming: 0,
},
};

/** tool_attachment rule table — `edge_kinds.tool_attachment.rules` in the JSON. */
export const TOOL_ATTACHMENT_RULES: Readonly<Record<NodeType, ConnectionRule>> = {
  "agent": {
    allowed_source_types: new Set<NodeType>(["tool"]),
    allowed_target_types: new Set<NodeType>([]),
    max_outgoing: 0,
    min_outgoing: 0,
    min_incoming: 0,
    max_incoming: null,
},
  "tool": {
    allowed_source_types: new Set<NodeType>([]),
    allowed_target_types: new Set<NodeType>(["agent"]),
    max_outgoing: null,
    min_outgoing: 0,
    min_incoming: 0,
    max_incoming: 0,
},
};
