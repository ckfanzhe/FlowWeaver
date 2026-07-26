/**
 * Node icons — solid monochrome SVGs, one per v1 node type.
 *
 * Design rules:
 *   - All icons share the same viewBox (0 0 24 24) and stroke/fill weight
 *     so they look like one family.
 *   - They use `currentColor` so each node's `text-` token colours them.
 *   - Solid shapes (fill) where possible, with a few thin strokes for
 *     detail. No emoji, no multi-colour.
 *   - Lucide/Heroicons-style geometry for familiarity.
 */
import type { ReactNode } from 'react'

type IconProps = { className?: string }

function wrap(children: ReactNode, className?: string) {
  return (
    <svg
      className={className}
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  )
}

/** Agent — a robot head: rounded square + antenna + visor slot. */
export function AgentIcon({ className }: IconProps) {
  return wrap(
    <>
      <rect x="4" y="7" width="16" height="12" rx="3" fill="currentColor" stroke="none" />
      <path d="M12 3v4" />
      <circle cx="12" cy="3" r="1" fill="currentColor" stroke="none" />
      <line x1="8" y1="13" x2="16" y2="13" stroke="white" strokeWidth="1.25" />
      <line x1="3" y1="13" x2="4" y2="13" />
      <line x1="20" y1="13" x2="21" y2="13" />
    </>,
    className
  )
}

/** Branch — . Replaces `RouterIcon` + `ConditionIcon`.
 *  A diamond with a horizontal fork on the right: the diamond body
 *  evokes a decision gate (carried over from the prior RouterIcon /
 *  ConditionIcon shape), the right-side fork visually distinguishes
 *  it from a generic decision icon. The mode label ("Switch" / "If-Else")
 *  is rendered by the BranchNode body separately, so a single icon is
 *  enough. Distinct from `FlowIcon` (horizontal fan) and `LoopIcon`
 *  (circular arrow). */
export function BranchIcon({ className }: IconProps) {
  return wrap(
    <>
      <path
        d="M12 3l9 9-9 9-9-9z"
        fill="currentColor"
        stroke="currentColor"
      />
      {/* Right-side fork: split exit into two outgoing arrows */}
      <line x1="6" y1="12" x2="14" y2="12" stroke="white" strokeWidth="1.5" />
      <line x1="14" y1="12" x2="19" y2="8" stroke="white" strokeWidth="1.3" />
      <line x1="14" y1="12" x2="19" y2="16" stroke="white" strokeWidth="1.3" />
      <polyline points="17.5,9 19,7 20.5,9" stroke="white" strokeWidth="1.3" fill="none" />
      <polyline points="17.5,15 19,17 20.5,15" stroke="white" strokeWidth="1.3" fill="none" />
    </>,
    className
  )
}

/** Flow — . Replaces `ParallelIcon` + `StepsIcon`.
 *  A horizontal two-arrow fan with a connecting spine: the left side
 *  shows the entry node splitting into two branches (parallel), the
 *  right side shows a single arrow continuing downward (sequential).
 *  The mode label ("Parallel" / "Sequential") is rendered by the
 *  FlowNode body separately, so a single icon is enough.
 *  Distinct from `RouterIcon` (which has a decision diamond) and
 *  from `LoopIcon` (which is a single circular arrow). */
export function FlowIcon({ className }: IconProps) {
  return wrap(
    <>
      {/* Entry node */}
      <circle cx="6" cy="12" r="2.5" fill="currentColor" stroke="none" />
      {/* Spine (horizontal) into the fork */}
      <line x1="8.5" y1="12" x2="14" y2="12" />
      {/* Two parallel branches */}
      <line x1="14" y1="12" x2="20" y2="6" />
      <line x1="14" y1="12" x2="20" y2="18" />
      <polyline points="17.5,8 20,5 22.5,8" />
      <polyline points="17.5,16 20,19 22.5,16" />
    </>,
    className
  )
}

/** Ask — a speech bubble with a question mark. :
 *  renamed from `HumanInputIcon`; visual unchanged. */
export function AskIcon({ className }: IconProps) {
  return wrap(
    <>
      <path
        d="M4 5h16a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H9l-4 4V6a1 1 0 0 1 1-1z"
        fill="currentColor"
        stroke="currentColor"
      />
      <text
        x="12"
        y="15"
        textAnchor="middle"
        fontSize="9"
        fontWeight="700"
        fill="white"
        stroke="none"
        fontFamily="system-ui, sans-serif"
      >
        ?
      </text>
    </>,
    className
  )
}

/** Tool — : a wrench (carried over verbatim from
 *  the deleted `ToolsIcon`). The `source` discriminator in the config
 *  is what picks which tool-emit primitive the runtime builds; the
 *  icon stays neutral so the source badge in the node body makes the
 *  active mode unambiguous.
 *
 *  : the same wrench now also stands in for
 *  the 5 collapsed preset tool types (wikipedia / tavily_search /
 *  duckduckgo / calculator / arxiv_search). The body's preset
 *  badge (`tool preset: <name>`) carries the distinction, so the
 *  canvas icon stays neutral. The previous per-preset icons
 *  (TavilyIcon / DuckDuckGoIcon / CalculatorIcon / ArxivIcon /
 *  WikipediaIcon) are deleted.
 */
export function ToolIcon({ className }: IconProps) {
  return wrap(
    <>
      <path
        d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.5-2.5z"
        fill="currentColor"
        stroke="currentColor"
      />
    </>,
    className
  )
}

/** Loop — two arrows forming a circular flow, suggests iteration. */
export function LoopIcon({ className }: IconProps) {
  return wrap(
    <>
      {/* top arc going right */}
      <path
        d="M5 9a7 7 0 0 1 12 0"
        fill="none"
        stroke="currentColor"
      />
      {/* bottom arc going left */}
      <path
        d="M19 15a7 7 0 0 1-12 0"
        fill="none"
        stroke="currentColor"
      />
      {/* arrowheads */}
      <path d="M14 6l3 3-3 3" fill="currentColor" stroke="none" />
      <path d="M10 18l-3-3 3-3" fill="currentColor" stroke="none" />
    </>,
    className
  )
}

/** Knowledge — open book with a small dot, evokes "things the agent
 *  has read". New in [[gleaming-munching-grove]] — RAG / vector DB
 *  sources for an agent's `knowledge=...` parameter. Visually
 *  distinct from `ToolIcon` (wrench) — knowledge isn't a tool call,
 *  it's retrieval context. The body renders the chosen vector DB +
 *  embedder + source-count badges separately, so a single icon is
 *  enough. */
export function KnowledgeIcon({ className }: IconProps) {
  return wrap(
    <>
      {/* Open book — two pages joined at a center spine */}
      <path
        d="M3 5h8a2 2 0 0 1 2 2v13a1 1 0 0 0-1-1H3V5z"
        fill="currentColor"
        stroke="currentColor"
      />
      <path
        d="M21 5h-8a2 2 0 0 0-2 2v13a1 1 0 0 1 1-1h9V5z"
        fill="currentColor"
        stroke="currentColor"
      />
      {/* Three text-line strokes on the left page */}
      <line x1="5" y1="9" x2="10" y2="9" stroke="white" strokeWidth="1.2" />
      <line x1="5" y1="12" x2="10" y2="12" stroke="white" strokeWidth="1.2" />
      <line x1="5" y1="15" x2="9" y2="15" stroke="white" strokeWidth="1.2" />
      {/* Three text-line strokes on the right page */}
      <line x1="14" y1="9" x2="19" y2="9" stroke="white" strokeWidth="1.2" />
      <line x1="14" y1="12" x2="19" y2="12" stroke="white" strokeWidth="1.2" />
      <line x1="14" y1="15" x2="18" y2="15" stroke="white" strokeWidth="1.2" />
    </>,
    className
  )
}

// Icons keyed by the manifest's `icon` string
// (`AgentIcon`, `RouterIcon`, ...). The `nodeStyles.ts` consumer reads
// this to resolve the manifest entry's `icon` field into a React
// component. Keeping this map local to the icons file means new node
// types only need: (a) a manifest entry, (b) an exported component
// here.
//
// phase : removed the previous `ICON_MAP` keyed by
// `NodeType` and the dead `NodeIconByType` export — both were
// zero-callers after the phase manifest-driven rewrite. Callers
// that need a node's icon by type read `manifestToVisuals()` from
// `nodeStyles.ts` (the manifest already carries the icon-name string
// for every entry).
//
// phase : added `UnknownIcon` — the sentinel icon
// rendered when a manifest entry references an icon name that
// hasn't been registered here. `nodeStyles.entryToVisual` returns a
// `NodeVisual` pointing at this icon instead of throwing, so a
// single bad entry can no longer take the whole canvas down.
export const ICON_BY_MANIFEST_NAME: Record<string, (p: IconProps) => JSX.Element> = {
  AgentIcon,
  // : `RouterIcon` + `ConditionIcon` collapsed
  // to a single `BranchIcon` — the body renders the mode label
  // separately.
  BranchIcon,
  // : `ParallelIcon` + `StepsIcon` collapsed to
  // a single `FlowIcon` — the body renders the mode label separately.
  FlowIcon,
  LoopIcon,
  // : renamed from `HumanInputIcon`.
  AskIcon,
  // : `McpIcon` + `HttpIcon` + `ToolsIcon`
  // collapsed to a single `ToolIcon` — the body renders a `source`
  // label badge separately (per the manifest's `cfg.source` field).
  // : the 5 collapsed preset tool types
  // (wikipedia / tavily_search / duckduckgo / calculator /
  // arxiv_search) reuse the same ToolIcon — the body's preset
  // badge (`tool preset: <name>`) carries the distinction.
  ToolIcon,
  // : RAG / vector DB source.
  // The body shows vector DB kind + embedder kind + source count.
  KnowledgeIcon,
}

// Sentinel icon used by `nodeStyles.entryToVisual` when a manifest
// entry references an icon name we haven't registered. Keeps the
// canvas from going blank — the user sees a labelled placeholder
// for the broken node and the rest of the workflow keeps working.
// Production keeps this quiet; dev warns once via console.warn in
// `nodeStyles.ts`.
export function UnknownIcon({ className }: IconProps): JSX.Element {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      aria-hidden="true"
    >
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <path d="M9 9a3 3 0 1 1 4.5 2.6c-.9.5-1.5 1-1.5 2.4" />
      <circle cx="12" cy="17" r="1" fill="currentColor" stroke="none" />
    </svg>
  )
}
