/**
 * GENERATED FILE — DO NOT EDIT.
 *
 * Source of truth: backend/src/app/schemas/node_configs.py
 * Regenerate with:  python scripts/generate_node_configs_ts.py
 * CI check:         python scripts/check_node_configs_consistency.py
 *
 * row D : the per-node TS interfaces used to
 * live in `workflow.ts:99-330` as a hand mirror of the Pydantic
 * schemas. Drift was easy. This file is now codegen — adding a
 * field on the Python side requires re-running this script and
 * the TS picks it up. Both sides evolve together.
 *
 * Field keys use the Pydantic alias (camelCase) when present so
 * the TS matches the JSON wire format the frontend already
 * speaks (`modelId`, `toolsRef`, `requiresConfirmation`, ...).
 *
 * `workflow.ts` re-exports these types so existing imports
 * (`import type { AgentNodeConfig } from '../../types/workflow'`)
 * keep working unchanged.
 */

export interface ModelConfig {
  provider: string;
  modelId: string;
  apiKey?: string | null;
  baseUrl?: string | null;
  presetId?: string | null;
}

export interface ParamSchema {
  name: string;
  type: 'string' | 'number' | 'boolean' | 'object';
  description: string;
  required: boolean;
}

export interface ToolFunction {
  name: string;
  description: string;
  parameters: ParamSchema[];
  code: string;
}

export interface BranchTarget {
  label: string;
  target: string;
  condition?: string | null;
}

export interface RouterSelector {
  mode: 'function' | 'cel' | 'hitl';
  expression: string;
  fallbackMessage: string;
}

export interface ConditionEvaluator {
  mode: 'function' | 'cel' | 'literal';
  expression: string;
  migratedFromLegacy: boolean;
}

export interface AgentNodeConfig {
  model?: ModelConfig | null;
  instructions: string;
  toolsRef: string[];
  markdown: boolean;
  requiresConfirmation: string[];
  systemMessage: string;
  reasoning: boolean;
  reasoningModel?: ModelConfig | null;
  retries: number;
  delayBetweenRetries: number;
  toolCallLimit?: number | null;
  addDatetimeToContext: boolean;
  parserModel?: ModelConfig | null;
  parserModelPrompt: string;
  preHooks: string[];
  postHooks: string[];
}

export interface ToolNodeConfig {
  preset?: 'wikipedia' | 'tavily_search' | 'duckduckgo' | 'calculator' | 'arxiv_search' | null;
  source: 'mcp' | 'http' | 'function';
  toolName: string;
  toolDescription: string;
  method: 'GET' | 'POST';
  baseUrl: string;
  path: string;
  headers: Record<string, string>;
  queryParams: Record<string, string>;
  bodySchema: string;
  authToken: string;
  serverId: string;
  toolNamePrefix: string;
  functions: ToolFunction[];
  enabled_methods: string[];
  toolkit_options: Record<string, unknown>;
}

export interface BranchNodeConfig {
  mode: 'switch' | 'if-else';
  selector: RouterSelector;
  evaluator: ConditionEvaluator;
  elseTarget: string;
  requiresConfirmation: boolean;
  confirmationMessage: string;
  branches: BranchTarget[];
}

export interface FlowNodeConfig {
  mode: 'parallel' | 'sequential';
  branches: BranchTarget[];
  requiresConfirmation: boolean;
  confirmationMessage: string;
}

export interface LoopNodeConfig {
  maxIterations: number;
  endCondition: string;
  forwardIterationOutput: boolean;
  bodyTarget: string;
  requiresConfirmation: boolean;
  confirmationMessage: string;
  requiresIterationReview: boolean;
  iterationReviewMessage: string;
}

export interface AskConfig {
  prompt: string;
  inputType: 'text' | 'confirm' | 'choice';
  choices: string[];
}

export type NodeConfig =
    AgentNodeConfig
  | ToolNodeConfig
  | BranchNodeConfig
  | FlowNodeConfig
  | LoopNodeConfig
  | AskConfig
;
