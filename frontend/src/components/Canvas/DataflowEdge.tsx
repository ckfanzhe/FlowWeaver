/**
 * DataflowEdge — animated edge for control-flow wiring between
 * nodes (agent → agent, agent → router, condition → agent, etc.).
 *
 * Path shape: a single cubic-bezier curve from the source handle to
 * the target handle. Same shape as React Flow's default `default`
 * edge — chosen because it reads naturally for short hops between
 * adjacent nodes and for long diagonal flows across the canvas.
 * (Smooth-step / right-angle paths were tried first; the 90° folds
 * fought with the dashed flow and made the direction hard to read.)
 *
 * Direction: the path is drawn from `sourceX/Y` to `targetX/Y` so
 * the dashes drift source → target along that exact curve. Even if
 * a user wires a node "backwards" (target.handle → source.handle),
 * the visual flow still points the way the workflow actually
 * evaluates, because `WorkflowCanvas.onConnect` records whichever
 * end the user dragged FROM as the source.
 *
 * Why SVG `<animate>` instead of CSS keyframes:
 *   Earlier revisions applied the motion via a CSS
 *   `@keyframes edge-dash-flow` rule referenced from an inline
 *   `style={{ animation: '…' }}`. The animation never appeared at
 *   runtime — most likely the keyframe definition was tree-shaken
 *   by Vite's CSS pipeline (CSS rules referenced only from JS-
 *   computed `style.animation` strings don't always survive purging).
 *   SVG SMIL `<animate>` is parsed and executed by the browser's
 *   SVG renderer directly; it doesn't need any CSS, any class, or
 *   any keyframe definition. The motion is therefore guaranteed
 *   to run as long as the browser supports SVG (every modern one
 *   does, including the WebKit builds used in this app).
 *
 * Tool-attachment edges (`tool_source → agent`) use a separate
 * `ToolAttachmentEdge` so they stay visually distinct — they're a
 * wiring hint, not a flow step.
 */
import { memo } from 'react'
import {
  EdgeLabelRenderer,
  getBezierPath,
  type EdgeProps,
} from '@xyflow/react'

export type DataflowEdgeProps = EdgeProps

/**
 * One full cycle of the dash flow. With `stroke-dasharray: "8 4"`
 * the pattern is 12px long, so animating the offset by 12 over
 * 0.6s gives a steady ~20 px/s drift — fast enough to read as
 * "moving forward" without being distracting on a static canvas.
 */
const ANIM_DURATION_S = 0.6
const ANIM_OFFSET_PX = 12

function DataflowEdgeComponent({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  selected,
}: DataflowEdgeProps) {
  const [edgePath, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  })

  // Stroke styling is set as inline `style` on the path so it can't
  // be overridden by React Flow's `.react-flow__edge-path` defaults
  // (those use CSS variables and would otherwise paint over our
  // colours and dash pattern).
  const stroke = selected ? 'rgb(var(--accent-text))' : 'rgb(var(--text-muted))'
  const strokeWidth = selected ? 2.5 : 2

  return (
    <>
      <path
        id={id}
        d={edgePath}
        fill="none"
        className={
          selected
            ? 'react-flow__edge-path react-flow__edge-dataflow react-flow__edge-dataflow--selected'
            : 'react-flow__edge-path react-flow__edge-dataflow'
        }
        style={{
          stroke,
          strokeWidth,
          strokeDasharray: '8 4',
        }}
      >
        {/* SMIL animation — moves the dash pattern along the path.
            This is the actual flow cue; everything above is just
            visual styling. */}
        <animate
          attributeName="stroke-dashoffset"
          from="0"
          to={-ANIM_OFFSET_PX}
          dur={`${ANIM_DURATION_S}s`}
          repeatCount="indefinite"
        />
      </path>
      {/* Invisible wider stroke that captures pointer events for
          click/hover. Mirrors what `BaseEdge` does internally so we
          don't lose edge interactivity by replacing `BaseEdge`. */}
      <path
        d={edgePath}
        fill="none"
        stroke="transparent"
        strokeWidth={20}
        className="react-flow__edge-interaction"
      />
      <EdgeLabelRenderer>
        <div
          style={{
            position: 'absolute',
            transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
            pointerEvents: 'all',
          }}
          className="nodrag nopan"
        />
      </EdgeLabelRenderer>
    </>
  )
}

export const DataflowEdge = memo(DataflowEdgeComponent)