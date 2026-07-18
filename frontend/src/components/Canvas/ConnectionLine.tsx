/**
 * ConnectionLine — custom connection drag preview.
 *
 * React Flow's default ConnectionLine draws a plain straight line from
 * the source handle to the cursor. That works, but it feels
 * disconnected from the bezier + dashed `DataflowEdge` we render for
 * committed connections — the preview doesn't look like the thing it
 * will become once the user releases.
 *
 * This component closes that gap:
 *   - same bezier curve grammar as `DataflowEdge` (`getBezierPath`),
 *     so the preview looks like a real edge in flight;
 *   - same SMIL `<animate>` flowing dashes (drift toward the cursor)
 *     so the preview reads as "this wire is being routed here";
 *   - color flips to a destructive tone (`invalid`) when the current
 *     `isValidConnection` callback would reject the drop, so the user
 *     sees WHY before releasing.
 *
 * Why SMIL over a CSS keyframe:
 *   Same rationale as `DataflowEdge` — the keyframe gets tree-shaken
 *   by Vite because it's only referenced from a JS-computed
 *   `style.animation` string. SMIL is parsed by the SVG renderer and
 *   doesn't need any CSS plumbing.
 *
 * Re-renders:
 *   The component is fed fresh `fromX/Y` + `toX/Y` (cursor) + validity
 *   on every mousemove, so React unmounts/remounts the `<animate>`
 *   each tick. That's fine — SMIL animations re-run from their
 *   starting offset on mount, so the dashes appear to keep flowing
 *   forward instead of stuttering backwards.
 */
import {
  getBezierPath,
  type ConnectionLineComponentProps,
} from '@xyflow/react'

const ANIM_DURATION_S = 0.6
const ANIM_OFFSET_PX = 12

export function ConnectionLine({
  fromX,
  fromY,
  fromPosition,
  toX,
  toY,
  toPosition,
  connectionStatus,
}: ConnectionLineComponentProps) {
  const [edgePath] = getBezierPath({
    sourceX: fromX,
    sourceY: fromY,
    sourcePosition: fromPosition,
    targetX: toX,
    targetY: toY,
    targetPosition: toPosition,
  })

  // Destructive tone when the drop would be rejected — the same red
  // we use elsewhere for error affordances. `valid` and unknown both
  // use the muted text colour so a preview that's about to be accepted
  // doesn't shout at the user.
  const stroke =
    connectionStatus === 'invalid'
      ? 'rgb(var(--danger))'
      : 'rgb(var(--text-muted))'

  return (
    <g>
      <path
        d={edgePath}
        fill="none"
        className="react-flow__edge-path react-flow__edge-connection"
        style={{
          stroke,
          strokeWidth: 2,
          strokeDasharray: '8 4',
        }}
      >
        <animate
          attributeName="stroke-dashoffset"
          from="0"
          to={-ANIM_OFFSET_PX}
          dur={`${ANIM_DURATION_S}s`}
          repeatCount="indefinite"
        />
      </path>
      {/* Invisible wider hit-target so the user can drop near (but
          not exactly on) the line. Mirrors the interaction layer we
          add to committed edges in `DataflowEdge`. */}
      <path
        d={edgePath}
        fill="none"
        stroke="transparent"
        strokeWidth={20}
        className="react-flow__edge-interaction"
      />
    </g>
  )
}