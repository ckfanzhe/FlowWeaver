/**
 * ToolAttachmentEdge — static dashed edge for tool wiring
 * (`tool_source → agent`). Tool sources are configuration nodes (a
 * function definition, an MCP server, an HTTP endpoint) — they feed
 * capability into an agent but aren't part of the workflow's control
 * flow.
 *
 * Visually distinct from `DataflowEdge`:
 *   * thinner stroke + lighter color → looks like a wire, not a path
 *   * no animation → reading the canvas shouldn't require tracking
 *     multiple moving lines; the dashes that ARE present are a
 *     static "this is a wiring hint" cue
 *
 * Path shape: same bezier as `DataflowEdge` so a mixed canvas reads
 * as one consistent style (right-angle folds were tried for tool
 * wires too, but the visual mismatch with the bezier dataflow edges
 * was jarring; the user reads the canvas better when all wires
 * share the same curve grammar).
 *
 * The arrowhead is still drawn so the connection direction is
 * unambiguous when a user accidentally drags a wire backwards.
 *
 * Stroke / dasharray are applied as inline `style` so React Flow's
 * default `.react-flow__edge-path { stroke: var(--xy-edge-stroke, ...) }`
 * can't override them — see DataflowEdge for the rationale.
 */
import { memo } from 'react'
import {
  getBezierPath,
  type EdgeProps,
} from '@xyflow/react'

export type ToolAttachmentEdgeProps = EdgeProps

function ToolAttachmentEdgeComponent({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  selected,
}: ToolAttachmentEdgeProps) {
  const [edgePath] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  })

  return (
    <>
      <path
        id={id}
        d={edgePath}
        fill="none"
        className={
          selected
            ? 'react-flow__edge-path react-flow__edge-tool-attachment react-flow__edge-tool-attachment--selected'
            : 'react-flow__edge-path react-flow__edge-tool-attachment'
        }
        style={{
          strokeDasharray: '4 4',
          stroke: selected ? 'rgb(var(--accent-text))' : 'rgb(var(--border-strong))',
          strokeWidth: selected ? 2 : 1.5,
          opacity: selected ? 1 : 0.85,
        }}
      />
      {/* Invisible wider stroke that captures pointer events. */}
      <path
        d={edgePath}
        fill="none"
        stroke="transparent"
        strokeWidth={20}
        className="react-flow__edge-interaction"
      />
    </>
  )
}

export const ToolAttachmentEdge = memo(ToolAttachmentEdgeComponent)