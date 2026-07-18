/**
 * MarkdownText — markdown renderer used as the `Text` slot of
 * `MessagePrimitive.Parts`.
 *
 * For assistant messages we wrap the prose in a native chat bubble
 * (subtle surface fill, edge border, soft shadow) so the LLM's text
 * reads as a discrete unit instead of floating naked text alongside
 * the tool-call / diff cards. For user messages the bubble chrome
 * already lives in `MessageBubbleForRole` (the accent-fill pill), so
 * we render the prose bare inside that bubble — wrapping it again
 * would create the dreaded nested-bubble artefact.
 *
 * The role is read from `ChatRoleContext`, which `MessageBubbleForRole`
 * sets at the top of every message.
 *
 * Features:
 *   - `react-markdown` + `remark-gfm` so tables, task lists,
 *     strikethrough, and autolinks work.
 *   - Custom `a`, `code`, `pre`, `table` components — they need
 *     theme-token styling the framework defaults don't supply.
 *   - Everything else (`p`, `h1-h6`, `ul`, `ol`, `li`, `blockquote`,
 *     `strong`, `em`, `del`, `br`, `hr`, …) is styled via CSS under
 *     `.chat-prose` in `index.css`.
 */
import { useContext } from 'react'
import type {
  AnchorHTMLAttributes,
  FC,
  HTMLAttributes,
  TableHTMLAttributes,
} from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { TextMessagePartComponent } from '@assistant-ui/react'
import { ChatRoleContext } from './chatRoleContext'

const MarkdownText: TextMessagePartComponent = ({ text }) => {
  const role = useContext(ChatRoleContext)
  if (!text) return null
  const body = (
    <div className="chat-prose">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: AnchorLink,
          code: CodeOrInline,
          pre: PreformattedBlock,
          table: MarkdownTable,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
  // User messages: bubble already in MessageBubbleForRole (accent fill).
  // Render bare — don't add a second wrapper.
  if (role === 'user') return body
  // Assistant messages: native bubble — same accent-strip style as the
  // other assistant data parts (Confirmation / Completed / Error). The
  // `flex justify-start mb-2` keeps it pinned to the left edge with the
  // rest of the assistant message.
  return (
    <div className="flex justify-start mb-2">
      <div className="chat-prose-bubble max-w-[90%] min-w-0 rounded-r-md rounded-l-sm border border-edge bg-bubble-bg px-3 py-2 text-sm text-ink break-words overflow-wrap-anywhere shadow-sm">
        {body}
      </div>
    </div>
  )
}

export default MarkdownText

// ─────────────────────────────────────────────────────────────────
// Custom renderers
// ─────────────────────────────────────────────────────────────────

const AnchorLink: FC<AnchorHTMLAttributes<HTMLAnchorElement>> = ({
  href,
  children,
  ...rest
}) => (
  <a
    href={href}
    target="_blank"
    rel="noreferrer noopener"
    className="text-accent-text underline decoration-accent/40 underline-offset-2 hover:decoration-accent-text transition-colors"
    {...rest}
  >
    {children}
  </a>
)

/**
 * `react-markdown` passes `inline` to the `code` renderer so we can
 * branch into inline vs block styles from one component.
 */
const CodeOrInline: FC<HTMLAttributes<HTMLElement> & { inline?: boolean }> = ({
  inline,
  className,
  children,
  ...rest
}) => {
  if (inline) {
    return (
      <code
        className="font-mono text-[0.78em] px-1 py-0.5 rounded bg-surface-2 text-ink before:content-none after:content-none"
        {...rest}
      >
        {children}
      </code>
    )
  }
  // Block code: hand off to <pre> via a wrapping span so the parent's
  // `pre` override picks it up. We don't apply the bg here — the
  // `pre` override owns the block-level styling.
  return (
    <code
      className={['font-mono text-[12px]', className].filter(Boolean).join(' ')}
      {...rest}
    >
      {children}
    </code>
  )
}

const PreformattedBlock: FC<HTMLAttributes<HTMLPreElement>> = ({
  children,
  ...rest
}) => (
  <pre
    className="my-2 overflow-x-auto rounded-md border border-edge bg-surface-2 px-2.5 py-2 text-[12px] leading-snug font-mono"
    {...rest}
  >
    {children}
  </pre>
)

/**
 * GFM tables — compact columns with the same border token as the
 * bubble itself, so the table reads as part of the bubble's chrome.
 */
const MarkdownTable: FC<TableHTMLAttributes<HTMLTableElement>> = ({
  children,
  ...rest
}) => (
  <div className="my-2 overflow-x-auto">
    <table className="w-full text-[12px] border-collapse" {...rest}>
      {children}
    </table>
  </div>
)