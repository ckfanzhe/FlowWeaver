/**
 * Assistant-runtime adapters — bridge our zustand chat stores to
 * assistant-ui's `ExternalStoreAdapter`.
 *
 * Two adapters live here:
 *
 *   - `createRunModeAdapter`  — bridges the runtime chat (execute
 *     mode). Translates `ChatMessage` (user / text / tool_call /
 *     tool_result / confirmation / completed / error) into
 *     `ThreadMessageLike` parts. Calls `dispatchSend` / `dispatchAnswer`
 *     on `onNew`.
 *
 *   - `createBuildModeAdapter` — bridges the builder chat (build
 *     mode). Translates `BuilderChatMessage` (user / thinking /
 *     text / tool_call / tool_result / diff / completed / error)
 *     into `ThreadMessageLike` parts. Calls `dispatchSend`
 *     (the builder's send) on `onNew`.
 *
 * Both adapters ignore `setMessages`, `onEdit`, `onDelete`, and
 * `onReload` — the chat is append-only and managed by our own
 * reducer pipeline (SSE → reducer → zustand → adapter snapshot).
 *
 * Part-type choices (assistant-ui best practice):
 *
 *   - `text` parts → native `TextMessagePart`. The framework's default
 *     `Text` slot renders `<p>` with `whiteSpace: pre-line` and a
 *     smooth-stream view. We don't override the slot — sizing is
 *     controlled by CSS on the chat surface.
 *
 *   - `thinking` parts → native `ReasoningMessagePart`. The framework
 *     has a dedicated `Reasoning:` slot for these. We render a small
 *     spinner in that slot.
 *
 *   - `tool_call` / `tool_result` parts → native `ToolCallMessagePart`.
 *     This is the framework's first-class tool-call shape. Consecutive
 *     tool-call parts are auto-grouped by the framework's `ToolGroup`
 *     slot — no extra reducer work needed for the "list of tools"
 *     layout the user asked for.
 *
 *   - `diff` / `confirmation` / `completed` / `error` → custom
 *     `DataMessagePart` (the framework's typed-payload channel). The
 *     framework's `data.by_name` slot dispatches each by name.
 */
import { useMemo } from 'react'
import type {
  AppendMessage,
  ExternalStoreAdapter,
  MessageStatus,
  ThreadMessageLike,
  ToolCallMessagePart,
  TextMessagePart,
  ReasoningMessagePart,
} from '@assistant-ui/react'
import type { ChatMessage as RuntimeChatMessage } from '../store/sseClient'
import type { BuilderChatMessage } from '../types/chatBuilder'
import { dispatchSend as dispatchRuntimeSend, dispatchAnswer as dispatchRuntimeAnswer } from '../store/chatActions'
import { dispatchSend as dispatchBuilderSend } from '../store/builderChatActions'

// ───────────────────────────────────────────────────────────────
// Run-mode adapter
// ───────────────────────────────────────────────────────────────

export interface RunModeAdapterOpts {
  /** Live workflow id — `onNew` refuses to send without one. */
  workflowId: string | null
  /** Snapshot of the runtime message transcript. */
  messages: readonly RuntimeChatMessage[]
  /** Whether a stream is in flight. */
  busy: boolean
  /** Live prompt — non-null while waiting on user input. */
  hasPendingConfirmation: boolean
}

export function createRunModeAdapter(
  opts: RunModeAdapterOpts,
): ExternalStoreAdapter<RuntimeChatMessage> {
  return {
    isRunning: opts.busy,
    messages: opts.messages,

    onNew: async (msg: AppendMessage) => {
      const text = extractText(msg.content)
      if (!text) return
      if (!opts.workflowId) return
      if (opts.hasPendingConfirmation) {
        await dispatchRuntimeAnswer(text)
      } else {
        await dispatchRuntimeSend(opts.workflowId, text)
      }
    },

    convertMessage: convertRuntimeMessage,
  }
}

// ───────────────────────────────────────────────────────────────
// Build-mode adapter
// ───────────────────────────────────────────────────────────────

export interface BuildModeAdapterOpts {
  workflowId: string | null
  messages: readonly BuilderChatMessage[]
  busy: boolean
}

export function createBuildModeAdapter(
  opts: BuildModeAdapterOpts,
): ExternalStoreAdapter<BuilderChatMessage> {
  return {
    isRunning: opts.busy,
    messages: opts.messages,

    onNew: async (msg: AppendMessage) => {
      const text = extractText(msg.content)
      if (!text) return
      if (!opts.workflowId) return
      await dispatchBuilderSend(opts.workflowId, text)
    },

    convertMessage: convertBuilderMessage,
  }
}

// ───────────────────────────────────────────────────────────────
// Pure converters
// ───────────────────────────────────────────────────────────────

function extractText(content: AppendMessage['content']): string {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return content
    .map((p) => {
      if (typeof p === 'string') return p
      if (p && typeof p === 'object' && 'text' in p) {
        return String((p as { text: string }).text)
      }
      return ''
    })
    .filter(Boolean)
    .join('\n')
}

/**
 * Convert one runtime `ChatMessage` into a `ThreadMessageLike`.
 */
export function convertRuntimeMessage(
  m: RuntimeChatMessage,
  idx: number,
): ThreadMessageLike {
  const base = messageBase(m.id, idx)
  switch (m.kind) {
    case 'user':
      return { ...base, role: 'user', content: [textPart(textOf(m.data))] }
    case 'text':
      return { ...base, role: 'assistant', content: [textPart(textOf(m.data))] }
    case 'tool_call':
      return {
        ...base,
        role: 'assistant',
        content: [toolCallPart(m.data)],
      }
    case 'tool_result':
      return {
        ...base,
        role: 'assistant',
        content: [toolResultPart(m.data)],
      }
    case 'confirmation':
      return {
        ...base,
        role: 'assistant',
        content: [
          dataPart('confirmation', {
            kind: (m.data as { kind?: string }).kind ?? 'tool_confirm',
            prompt: (m.data as { prompt?: string }).prompt ?? '',
            choices: (m.data as { choices?: string[] }).choices ?? [],
          }),
        ],
      }
    case 'completed':
      return {
        ...base,
        role: 'assistant',
        status: { type: 'complete', reason: 'stop' } as MessageStatus,
        content: [dataPart('completed', { text: textOf(m.data) })],
      }
    case 'error':
      return {
        ...base,
        role: 'assistant',
        status: { type: 'incomplete', reason: 'error' } as MessageStatus,
        content: [
          dataPart('error', {
            message: (m.data as { message?: string }).message ?? 'error',
          }),
        ],
      }
    default:
      return { ...base, role: 'assistant', content: '' }
  }
}

/**
 * Convert one builder `BuilderChatMessage` into a `ThreadMessageLike`.
 */
export function convertBuilderMessage(
  m: BuilderChatMessage,
  idx: number,
): ThreadMessageLike {
  const base = messageBase(m.id, idx)
  switch (m.kind) {
    case 'user':
      return { ...base, role: 'user', content: [textPart(textOf(m.data))] }
    case 'thinking':
      // Native reasoning part — the framework's `Reasoning:` slot
      // renders it. We mark the message as running so the framework
      // also shows its in-progress indicator.
      return {
        ...base,
        role: 'assistant',
        status: { type: 'running' } as MessageStatus,
        content: [reasoningPart('')],
      }
    case 'text':
      return { ...base, role: 'assistant', content: [textPart(textOf(m.data))] }
    case 'tool_call':
      return {
        ...base,
        role: 'assistant',
        content: [toolCallPart(m.data)],
      }
    case 'tool_result':
      return {
        ...base,
        role: 'assistant',
        content: [toolResultPart(m.data)],
      }
    case 'diff':
      return {
        ...base,
        role: 'assistant',
        content: [dataPart('diff', m.data as Record<string, unknown>)],
      }
    case 'completed':
      return {
        ...base,
        role: 'assistant',
        status: { type: 'complete', reason: 'stop' } as MessageStatus,
        content: [dataPart('completed', { text: textOf(m.data) })],
      }
    case 'error':
      return {
        ...base,
        role: 'assistant',
        status: { type: 'incomplete', reason: 'error' } as MessageStatus,
        content: [
          dataPart('error', {
            message: (m.data as { message?: string }).message ?? 'error',
          }),
        ],
      }
    case 'retry':
      // Mid-stream retry notice — the LLM stream hit a transient
      // SSE-parser JSONDecodeError and the backend is restarting
      // the run. Rendered as a small inline chip; the actual
      // recovery events from the new attempt follow below in the
      // same turn. Keep `status: running` so the framework's
      // in-progress indicator stays alive.
      return {
        ...base,
        role: 'assistant',
        status: { type: 'running' } as MessageStatus,
        content: [
          dataPart('retry', {
            reason: (m.data as { reason?: string }).reason ?? '',
          }),
        ],
      }
    default:
      return { ...base, role: 'assistant', content: '' }
  }
}

// ───────────────────────────────────────────────────────────────
// Part builders
// ───────────────────────────────────────────────────────────────

function messageBase(id: string, idx: number) {
  return {
    // Empty / missing ids fall back to a deterministic `m-{idx}`
    // so the renderer always has a stable React key.
    id: id || `m-${idx}`,
    createdAt: new Date(),
  }
}

function textOf(data: Record<string, unknown>): string {
  return String(
    (data as { text?: string; content?: string }).text ??
      (data as { content?: string }).content ??
      '',
  )
}

/**
 * Strip a leading decorative bullet the LLM sometimes emits at the
 * start of a reply (`●`, `•`, `*`, `-`, `_`). Without this the chat
 * surface displays a stray bullet on its own line before the actual
 * paragraph. Applied at the adapter boundary so consumer code never
 * needs to defend against it.
 */
function normalizeText(raw: string): string {
  let t = raw.replace(/^\s+/, '')
  t = t.replace(/^([-*•●▪◦·_]\s*)+/, '')
  return t.trim()
}

function textPart(text: string): TextMessagePart {
  return { type: 'text', text: normalizeText(text) }
}

function reasoningPart(text: string): ReasoningMessagePart {
  return { type: 'reasoning', text: normalizeText(text) }
}

function dataPart(name: string, data: Record<string, unknown>) {
  return { type: 'data' as const, name, data }
}

/**
 * Build a native tool-call part from a `tool_call` SSE event. The
 * `toolCallId` is required by the framework's tool-call shape — we
 * fall back to a deterministic id when the backend omits one (legacy
 * events that predate the id field).
 *
 * `argsText` is the raw JSON the LLM streamed; we serialise `args`
 * back to JSON so the framework's loading indicator can render
 * partial parses progressively.
 */
function toolCallPart(data: Record<string, unknown>): ToolCallMessagePart {
  const toolCallId =
    (data as { tool_call_id?: string }).tool_call_id ?? ''
  const toolName = (data as { tool?: string }).tool ?? ''
  const args = (data as { args?: Record<string, unknown> }).args ?? {}
  return {
    type: 'tool-call',
    toolCallId: toolCallId || `tc-${hash(toolName + JSON.stringify(args))}`,
    toolName,
    args,
    argsText: JSON.stringify(args),
  } as ToolCallMessagePart
}

/**
 * Build a native tool-call part from a `tool_result` SSE event. The
 * framework's `ToolCallMessagePart` carries the result inline (in
 * `result` / `isError`), so we emit ONE part per result — the same
 * shape the framework uses for an in-flight call that just completed.
 *
 * The result is rendered directly inside the tool-call component.
 *
 * `ok` defaults to `true` (not `false`) — the runtime stream
 * explicitly populates `ok` from agno's `tool_call_error` flag, so
 * "missing" only arises for legacy events that predate the field.
 * Defaulting to `false` made every tool call show ✗ (the 
 * `dispatch_task` export bug — the call returned
 * `{success: True, task_id: ...}` but the runtime emitted `tool_result`
 * WITHOUT `ok`, so the frontend's `?? false` fallback fired).
 */
function toolResultPart(data: Record<string, unknown>): ToolCallMessagePart {
  const toolCallId =
    (data as { tool_call_id?: string }).tool_call_id ?? ''
  const toolName = (data as { tool?: string }).tool ?? ''
  // 1. Backend's explicit `ok` (the runtime now forwards
  //    `tool_call_error` from agno — see `event_adapter.py`).
  // 2. Some tools return `{success: true}` in their payload — the
  //    raw `result` field — and the runtime stores it on the
  //    event. Treat `success === false` as a failure
  //    (catch the wire drift where the backend hasn't been updated
  //    yet to forward `ok`).
  // 3. Default to `true`: a present `result` payload is the
  //    strongest signal we have, and `?? false` made every call
  //    show ✗.
  let ok = (data as { ok?: boolean }).ok
  if (ok === undefined) {
    const resultRaw = (data as { result?: unknown }).result
    if (
      resultRaw &&
      typeof resultRaw === 'object' &&
      'success' in (resultRaw as Record<string, unknown>)
    ) {
      ok = Boolean((resultRaw as { success: unknown }).success)
    } else {
      ok = true
    }
  }
  const message = (data as { message?: string }).message ?? ''
  const resultRaw = (data as { result?: unknown }).result
  // The framework's `result` is `unknown` — we pass the raw payload
  // when present, otherwise the human-readable message string.
  const result = resultRaw !== undefined ? resultRaw : message
  return {
    type: 'tool-call',
    toolCallId: toolCallId || `tc-${hash(toolName + toolCallId)}`,
    toolName,
    args: {},
    argsText: '',
    result,
    isError: !ok,
  } as ToolCallMessagePart
}

/** Stable 32-bit hash for synthetic tool-call ids. Not crypto-secure;
 *  just enough entropy to keep tool-call ids unique across renders. */
function hash(s: string): string {
  let h = 5381
  for (let i = 0; i < s.length; i++) {
    h = ((h << 5) + h + s.charCodeAt(i)) | 0
  }
  return (h >>> 0).toString(36)
}

// ───────────────────────────────────────────────────────────────
// React hooks — wrap the factories so consumers can pass stable
// adapter references via `useMemo`. The hooks themselves just
// forward to the factory; they're convenience for consumers.
// ───────────────────────────────────────────────────────────────

export function useRunModeAdapter(
  workflowId: string | null,
  messages: readonly RuntimeChatMessage[],
  busy: boolean,
  hasPendingConfirmation: boolean,
): ExternalStoreAdapter<RuntimeChatMessage> {
  return useMemo(
    () =>
      createRunModeAdapter({ workflowId, messages, busy, hasPendingConfirmation }),
    [workflowId, messages, busy, hasPendingConfirmation],
  )
}

export function useBuildModeAdapter(
  workflowId: string | null,
  messages: readonly BuilderChatMessage[],
  busy: boolean,
): ExternalStoreAdapter<BuilderChatMessage> {
  return useMemo(
    () => createBuildModeAdapter({ workflowId, messages, busy }),
    [workflowId, messages, busy],
  )
}
