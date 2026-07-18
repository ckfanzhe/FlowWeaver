/**
 * Message part renderer — bridge between our domain-specific
 * BuilderChatMessage kinds and assistant-ui's native part slots.
 *
 * Design philosophy (assistant-ui best practice):
 *
 *   - Use the framework's native part types wherever one exists.
 *     We emit `ToolCallMessagePart` for tool events (so the
 *     framework's `ToolGroup` slot auto-wraps consecutive calls),
 *     `ReasoningMessagePart` for thinking (so the framework's
 *     `Reasoning:` slot renders it), and `TextMessagePart` for
 *     LLM prose. The Text slot is provided by our `MarkdownText`
 *     component (react-markdown + remark-gfm) so the LLM's prose
 *     renders bold / lists / links / code instead of bare escaped
 *     text. Bubble chrome around the prose is owned by
 *     `MessageBubbleForRole` (user) or `MarkdownText` itself
 *     (assistant); see `chatRoleContext.ts`.
 *
 *   - One component per domain part slot. Each renderer is small
 *     and reads only the props the framework hands it — no
 *     zustand subscriptions, no business logic.
 *
 *   - Tool call renderers are dispatched by tool name via the
 *     `tools.by_name` registry. The framework handles the rest
 *     (running indicator, partial-args streaming, tool-group
 *     wrapping).
 */
import {
  MessagePrimitive,
  useMessagePartData,
  type ToolCallMessagePartProps,
  type DataMessagePartProps,
  type ReasoningMessagePartComponent,
} from '@assistant-ui/react'
import { useState, type FC, type PropsWithChildren } from 'react'
import { useT } from '../../i18n'
import type { BuilderDiff } from '../../types/chatBuilder'
import MarkdownText from './MarkdownText'

export interface ChatMessagePartsProps {
  /** Callback fired when the user clicks Apply on a diff card. */
  onApplyDiff?: () => void
  /** Callback fired when the user clicks Cancel on a diff card. */
  onCancelDiff?: () => void
}

export function ChatMessageParts({ onApplyDiff, onCancelDiff }: ChatMessagePartsProps) {
  return (
    <MessagePrimitive.Parts
      components={{
        // Text — full markdown rendering. The framework's default
        // Text slot is a bare <p> with `whiteSpace: pre-line`, so
        // LLM prose can't render bold / lists / links / code. We
        // override with a react-markdown renderer that respects
        // `remark-gfm` (tables, task lists, strikethrough,
        // autolinks). The bubble chrome is owned by
        // `MessageBubbleForRole` in ChatSidebar — this slot only
        // renders the prose body.
        Text: MarkdownText,
        // Reasoning: a tiny spinner while the LLM is thinking.
        // The framework's reasoning slot is null by default; we
        // override with our compact inline spinner. The framework
        // removes this once a text / tool-call event lands
        // (the message status flips from `running` to `complete`).
        Reasoning: ReasoningSpinner,
        // Tool call renderer — by tool name. The framework's
        // `ToolGroup` slot wraps consecutive tool calls into a
        // single visual region automatically.
        tools: {
          by_name: {
            add_node: ToolCallRow,
            update_node: ToolCallRow,
            remove_node: ToolCallRow,
            connect_nodes: ToolCallRow,
            disconnect: ToolCallRow,
            preview_workflow: ToolCallRow,
          },
          Fallback: ToolCallRow,
        },
        // Tool group wrapper — tight list, no card background.
        ToolGroup: ToolGroupList,
        // Domain-specific data parts (framework's typed-payload
        // channel). Slot by name.
        data: {
          by_name: {
            diff: (props) => (
              <DiffPart {...props} onApply={onApplyDiff} onCancel={onCancelDiff} />
            ),
            confirmation: ConfirmationPart,
            completed: CompletedPart,
            error: ErrorPart,
            retry: RetryPart,
          },
        },
      }}
    />
  )
}

// ───────────────────────────────────────────────────────────────
// Reasoning — frame-driven thinking indicator.
// ───────────────────────────────────────────────────────────────
const ReasoningSpinner: ReasoningMessagePartComponent = () => {
  return (
    <div className="flex justify-start mb-2">
      <div className="inline-flex items-center gap-1.5 text-[11px] font-mono text-ink-muted">
        <span className="inline-block w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse" />
        thinking…
      </div>
    </div>
  )
}

// ───────────────────────────────────────────────────────────────
// ToolCallRow — compact single-line tool call display.
//
// Per-tool specialised renderers (add_node shows the label,
// connect_nodes shows source→target, etc.) live in this single
// component. We read the tool name from the props and dispatch
// to the right summary helper.
//
// Render rules:
//   - No background, no border. A multi-call agent run produces
//     a list of one-line rows, not a stack of styled cards.
//   - Collapse the args payload by default ("Summary: node foo").
//     Click to expand and see the raw JSON.
//   - The result (when present) sits on the same row as a
//     ✓ / ✗ pill — also expandable.
// ───────────────────────────────────────────────────────────────
const ToolCallRow: FC<ToolCallMessagePartProps> = (props) => {
  const { toolName, args, result, isError, status } = props
  const running = status?.type === 'running'
  const summary = summarizeArgs(toolName, args as Record<string, unknown>)
  const resultText = resultToText(result)
  const [expanded, setExpanded] = useState(false)
  const hasArgs = args && Object.keys(args as object).length > 0
  const hasResult = resultText.length > 0
  return (
    <div className="flex justify-start mb-1.5">
      <div className="max-w-[90%] min-w-0 text-[11px] font-mono">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="group inline-flex items-center gap-1.5 text-ink-muted hover:text-ink transition-colors"
          aria-expanded={expanded}
        >
          <span className="opacity-50 w-3 inline-block text-center transition-transform group-hover:opacity-80">
            {expanded ? '▾' : '▸'}
          </span>
          <span className="opacity-70 group-hover:opacity-100">
            <WrenchIcon />
          </span>
          <b className="font-medium text-purple-700 dark:text-purple-300">{toolName}</b>
          {summary && <span className="opacity-70 truncate">{summary}</span>}
          {running && (
            <span className="ml-1 inline-block w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse" />
          )}
          {!running && hasResult && (
            <span
              className={[
                'ml-1 inline-flex items-center gap-1',
                isError
                  ? 'text-rose-700 dark:text-rose-300'
                  : 'text-emerald-700 dark:text-emerald-300',
              ].join(' ')}
            >
              {isError ? '✗' : '✓'}
            </span>
          )}
        </button>
        {expanded && (hasArgs || hasResult) && (
          <div className="mt-1 ml-5 text-ink-faint whitespace-pre-wrap break-words overflow-wrap-anywhere">
            {hasArgs && (
              <div>
                <span className="opacity-60">args</span>{' '}
                {formatArgs(args as Record<string, unknown>)}
              </div>
            )}
            {hasResult && (
              <div className="mt-0.5">
                <span className="opacity-60">{isError ? 'error' : 'result'}</span>{' '}
                {resultText}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// ───────────────────────────────────────────────────────────────
// ToolGroupList — the framework's ToolGroup wrapper. The default
// is a transparent pass-through; we keep the children stacked
// tightly so a run of 6 tool calls reads as a list, not a gap
// mountain. The framework supplies `startIndex` / `endIndex`
// for any consumers who want to render a header.
// ───────────────────────────────────────────────────────────────
function ToolGroupList({
  children,
}: PropsWithChildren<{ startIndex: number; endIndex: number }>) {
  return <div className="flex flex-col gap-0 mb-2">{children}</div>
}

// ───────────────────────────────────────────────────────────────
// DiffCard — the centerpiece of the builder chat. Same affordances
// as the pre-refactor DiffCard (summary chips, collapsible details,
// Apply / Cancel buttons). Reads the diff payload from the
// framework's typed data-part slot.
// ───────────────────────────────────────────────────────────────
const DiffPart: FC<DataMessagePartProps & { onApply?: () => void; onCancel?: () => void }> = ({
  onApply,
  onCancel,
}) => {
  const data = useDiffData()
  if (!data) return null
  return <DiffCard diff={data} onApply={onApply} onCancel={onCancel} />
}

function useDiffData(): BuilderDiff | null {
  // The framework exposes `useMessagePartData` for reading data
  // parts. The hook returns the entire `DataMessagePart` envelope
  // (`{ type: 'data', name: 'diff', data: { ... } }`) — we need the
  // inner `.data` field, which is the actual `BuilderDiff` payload
  // (summary, nodes, edges).
  const part = useMessagePartData() as
    | { data?: BuilderDiff }
    | null
    | undefined
  if (!part) return null
  const data = (part as { data?: BuilderDiff }).data
  return data ?? null
}

function DiffCard({
  diff,
  onApply,
  onCancel,
}: {
  diff: BuilderDiff
  onApply?: () => void
  onCancel?: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const s = diff.summary ?? {}
  const nodes = diff.nodes ?? []
  const edges = diff.edges ?? []
  const total =
    (s.added_nodes ?? 0) +
    (s.removed_nodes ?? 0) +
    (s.updated_nodes ?? 0) +
    (s.added_edges ?? 0) +
    (s.removed_edges ?? 0)
  const hasChanges = total > 0
  return (
    <div className="rounded-xl border border-edge bg-surface px-3.5 py-3 text-sm shadow-sm mb-3 overflow-hidden">
      {!hasChanges ? (
        <div className="flex items-center gap-2 text-xs text-ink-muted font-mono">
          <span className="opacity-70">○</span>
          <span>No changes to apply.</span>
        </div>
      ) : (
        <>
          <div className="flex items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-1.5 text-[11px] font-mono">
              {!!(s.added_nodes ?? 0) && (
                <span className="inline-flex items-center gap-1 rounded-full bg-emerald-100 dark:bg-emerald-900/40 px-2 py-0.5 text-emerald-700 dark:text-emerald-300">
                  +{s.added_nodes} nodes
                </span>
              )}
              {!!(s.removed_nodes ?? 0) && (
                <span className="inline-flex items-center gap-1 rounded-full bg-rose-100 dark:bg-rose-900/40 px-2 py-0.5 text-rose-700 dark:text-rose-300">
                  −{s.removed_nodes} nodes
                </span>
              )}
              {!!(s.updated_nodes ?? 0) && (
                <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 dark:bg-amber-900/40 px-2 py-0.5 text-amber-700 dark:text-amber-300">
                  ~{s.updated_nodes} nodes
                </span>
              )}
              {!!(s.added_edges ?? 0) && (
                <span className="inline-flex items-center gap-1 rounded-full bg-emerald-100 dark:bg-emerald-900/40 px-2 py-0.5 text-emerald-700 dark:text-emerald-300">
                  +{s.added_edges} edges
                </span>
              )}
              {!!(s.removed_edges ?? 0) && (
                <span className="inline-flex items-center gap-1 rounded-full bg-rose-100 dark:bg-rose-900/40 px-2 py-0.5 text-rose-700 dark:text-rose-300">
                  −{s.removed_edges} edges
                </span>
              )}
            </div>
            {(nodes.length > 0 || edges.length > 0) && (
              <button
                className="text-[11px] text-ink-muted hover:text-ink transition-colors"
                onClick={() => setExpanded((v) => !v)}
              >
                {expanded ? 'Hide' : 'Details'}
              </button>
            )}
          </div>
          {expanded && (
            <div className="mt-2 max-h-48 overflow-y-auto text-[11px] font-mono text-ink-muted space-y-1 border-t border-edge pt-2">
              {nodes.map((n, i) => (
                <NodeDiffRow key={`n-${i}`} n={n} />
              ))}
              {edges.map((e, i) => (
                <EdgeDiffRow key={`e-${i}`} e={e} />
              ))}
            </div>
          )}
          <div className="flex gap-2 justify-end mt-3">
            <button className="btn" onClick={onCancel}>
              Cancel
            </button>
            <button className="btn-primary" onClick={onApply}>
              Apply
            </button>
          </div>
        </>
      )}
    </div>
  )
}

function NodeDiffRow({ n }: { n: BuilderDiff['nodes'][number] }) {
  const id = (n.node ?? n.before ?? n.after ?? {}) as {
    id?: string
    type?: string
    data?: { label?: string }
  }
  const tag = n.op === 'added' ? '+' : n.op === 'removed' ? '−' : '~'
  return (
    <div className="flex items-center gap-2">
      <span
        className={[
          'inline-block w-4 text-center font-bold',
          n.op === 'added'
            ? 'text-emerald-700 dark:text-emerald-300'
            : n.op === 'removed'
              ? 'text-rose-700 dark:text-rose-300'
              : 'text-amber-700 dark:text-amber-300',
        ].join(' ')}
      >
        {tag}
      </span>
      <span className="text-ink">{id.data?.label ?? ''}</span>
      <span className="text-ink-faint">({id.type ?? ''})</span>
      <span className="text-ink-faint">{id.id ?? ''}</span>
    </div>
  )
}

function EdgeDiffRow({ e }: { e: BuilderDiff['edges'][number] }) {
  const edge = (e.edge ?? e.before ?? e.after ?? {}) as {
    source?: string
    target?: string
    kind?: string
  }
  const tag = e.op === 'added' ? '+' : e.op === 'removed' ? '−' : '~'
  return (
    <div className="flex items-center gap-2">
      <span
        className={[
          'inline-block w-4 text-center font-bold',
          n_op_color(e.op),
        ].join(' ')}
      >
        {tag}
      </span>
      <span className="text-ink">{edge.source ?? ''}</span>
      <span className="text-ink-faint">→</span>
      <span className="text-ink">{edge.target ?? ''}</span>
      {edge.kind && edge.kind !== 'dataflow' && (
        <span className="text-ink-faint">({edge.kind})</span>
      )}
    </div>
  )
}

function n_op_color(op: 'added' | 'removed' | 'updated') {
  if (op === 'added') return 'text-emerald-700 dark:text-emerald-300'
  if (op === 'removed') return 'text-rose-700 dark:text-rose-300'
  return 'text-amber-700 dark:text-amber-300'
}

// ───────────────────────────────────────────────────────────────
// Confirmation — yellow accent-strip bubble. The actual yes/no
// buttons live in the composer footer (the chat composer is the
// natural place for "answer" input).
// ───────────────────────────────────────────────────────────────
const ConfirmationPart: FC<DataMessagePartProps> = () => {
  const part = useMessagePartData() as
    | { data?: { kind?: string; prompt?: string; choices?: string[] } }
    | undefined
  const data = part?.data
  if (!data) return null
  const isHuman = data.kind === 'ask'
  return (
    <div className="flex justify-start mb-3">
      <div className="max-w-[85%] min-w-0 rounded-r-md rounded-l-sm border border-l-4 border-l-warning border-edge bg-warning-bg/60 px-3 py-2 text-sm text-warning break-words overflow-wrap-anywhere">
        <b>{isHuman ? 'Asking' : 'Confirm'}</b> {data.prompt}
        {data.choices && data.choices.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1 text-xs font-mono">
            {data.choices.map((c) => (
              <span key={c} className="rounded bg-warning/15 px-1.5 py-0.5">
                {c}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// ───────────────────────────────────────────────────────────────
// Completed — green accent-strip bubble. Skip if empty.
// ───────────────────────────────────────────────────────────────
const CompletedPart: FC<DataMessagePartProps> = () => {
  const part = useMessagePartData() as { data?: { text?: string } } | undefined
  const data = part?.data
  const text = data?.text?.trim() ?? ''
  if (!text) return null
  return (
    <div className="flex justify-start mb-3">
      <div className="inline-flex items-center gap-1.5 rounded-r-md rounded-l-sm border border-l-4 border-l-success border-edge bg-success-bg/60 px-3 py-1 text-xs text-success font-mono">
        <span className="opacity-80">✓</span>
        <span>{text}</span>
      </div>
    </div>
  )
}

// ───────────────────────────────────────────────────────────────
// Error — red accent-strip bubble. Skip if empty.
// ───────────────────────────────────────────────────────────────
const ErrorPart: FC<DataMessagePartProps> = () => {
  const part = useMessagePartData() as { data?: { message?: string } } | undefined
  const data = part?.data
  const message = data?.message?.trim() ?? ''
  if (!message) return null
  return (
    <div className="flex justify-start mb-3">
      <div className="max-w-[85%] min-w-0 rounded-r-md rounded-l-sm border border-l-4 border-l-danger border-edge bg-danger-bg/60 px-3 py-2 text-sm text-danger break-words overflow-wrap-anywhere">
        ✗ {message}
      </div>
    </div>
  )
}

// ───────────────────────────────────────────────────────────────
// Retry — small inline chip shown between the failed attempt's
// events and the recovered attempt's events. Mirrors the thinking
// spinner visually (same row, same font-mono treatment) so it
// reads as "still in progress, but the LLM hiccupped once".
//
// We don't surface the SDK error text in the chip — the user
// doesn't need to see "key must be a string at line 1 column
// 3984" to understand what's happening. The chip text is
// internationalised below.
// ───────────────────────────────────────────────────────────────
const RetryPart: FC<DataMessagePartProps> = () => {
  const t = useT()
  return (
    <div className="flex justify-start mb-2">
      <div
        className="inline-flex items-center gap-1.5 text-[11px] font-mono text-ink-muted"
        data-testid="chat-retry-chip"
      >
        <span className="inline-block w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse" />
        {t('chat.retry')}
      </div>
    </div>
  )
}

// ───────────────────────────────────────────────────────────────
// Pure helpers
// ───────────────────────────────────────────────────────────────

function formatArgs(args: Record<string, unknown> | undefined): string {
  if (!args || Object.keys(args).length === 0) return ''
  const allPrimitive = Object.values(args).every(
    (v) => v === null || ['string', 'number', 'boolean'].includes(typeof v),
  )
  if (allPrimitive) {
    return Object.entries(args)
      .map(([k, v]) =>
        v === null ? `${k}=null` : `${k}=${typeof v === 'string' ? JSON.stringify(v) : String(v)}`,
      )
      .join(', ')
  }
  return JSON.stringify(args, null, 2)
}

function summarizeArgs(tool: string, args: Record<string, unknown> | undefined): string {
  if (!args) return ''
  switch (tool) {
    case 'add_node': {
      const id = (args.id as string) ?? (args.node_id as string) ?? ''
      const type = (args.type as string) ?? ''
      const label = (args.label as string) ?? ''
      if (label) return `${type || 'node'} "${label}"`
      if (id) return `${type || 'node'} ${id}`
      return ''
    }
    case 'update_node':
    case 'remove_node': {
      const id = (args.id as string) ?? (args.node_id as string) ?? ''
      return id ? `node ${id}` : ''
    }
    case 'connect_nodes': {
      const s = (args.source as string) ?? ''
      const t = (args.target as string) ?? ''
      return s && t ? `${s} → ${t}` : ''
    }
    case 'disconnect': {
      const s = (args.source as string) ?? ''
      const t = (args.target as string) ?? ''
      return s && t ? `${s} ⤫ ${t}` : ''
    }
    case 'preview_workflow':
      return ''
    default: {
      const first = Object.entries(args).find(([, v]) => typeof v === 'string')
      if (first) {
        const val = first[1] as string
        return val.length > 30 ? val.slice(0, 30) + '…' : val
      }
      return ''
    }
  }
}

/** Stringify a tool result payload for inline display. The
 *  framework's `result` field is `unknown` — we handle the
 *  common shapes (string, object) and fall back to JSON. */
function resultToText(result: unknown): string {
  if (result == null) return ''
  if (typeof result === 'string') return result
  if (typeof result === 'number' || typeof result === 'boolean') {
    return String(result)
  }
  try {
    return JSON.stringify(result, null, 2)
  } catch {
    return '[unserialisable result]'
  }
}

// Small inline wrench icon — replaces the previous 🔧 emoji so
// tool rows render consistently across browsers / OSes / fonts.
function WrenchIcon() {
  return (
    <svg
      width="11"
      height="11"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className="inline-block"
    >
      <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
    </svg>
  )
}
