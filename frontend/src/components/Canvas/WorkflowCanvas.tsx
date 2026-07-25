/**
 * React Flow canvas wired to the workflow store.
 *  - drag from palette: drop creates a node
 *  - connect handles: creates an edge
 *  - select a node: store.selectedNodeId updates
 *  - delete key: removes selected node/edge
 *  - right-click: localized context menu (pane: add node / fit view;
 *                 node or edge: delete)
 *  - while dragging a connection: unreachable nodes are dimmed so
 *    the user can see which targets are valid before releasing.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Background,
  Controls,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Connection,
  type Edge,
  type Node,
  type NodeChange,
  type EdgeChange,
  applyEdgeChanges,
  applyNodeChanges,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'

import { useWorkflowStore, isConfigurable } from '../../store/workflowStore'
import { flowEdge, flowNode, resyncRfEdges, resyncRfNodes } from './canvasNodes'
import type { NodeType } from '../../types/workflow'
import { customNodeTypes } from '../Nodes'
import { CanvasContextMenu, type CanvasContextKind } from './CanvasContextMenu'
import { NodePalette } from './NodePalette'
import { DataflowEdge } from './DataflowEdge'
import { ToolAttachmentEdge } from './ToolAttachmentEdge'
import { t as i18n } from '../../i18n'
import {
  TOOL_SOURCE_TYPES,
  wouldBeValidConnection,
  type ConnectionError as RuleError,
  type EdgeKind,
} from '../../lib/connectionValidation'
import { isKnownNodeType } from '../../api/nodeTypes'
import { ConnectionLine } from './ConnectionLine'

/**
 * Registry of custom edge components. React Flow looks up the
 * component by the `type` string we set in `flowEdge`. Adding a new
 * kind here lets `WorkflowEdge.kind` route to the right renderer
 * without any change in the canvas below.
 */
const customEdgeTypes = {
  dataflow: DataflowEdge,
  tool_attachment: ToolAttachmentEdge,
} as const

/**
 * The visible width of a BaseNode (`min-w-[180px]` in BaseNode.tsx).
 * Used by the drop handler to center the new node on the cursor.
 */
const NODE_WIDTH = 180

/**
 * The header band height of a BaseNode — the row with the icon +
 * label at the top. Used by the drop handler to anchor the cursor
 * near the visible top-center of the new node.
 */
const NODE_HEADER_HEIGHT = 32

/**
 * Map a connection-rule error code to a localized i18n key that
 * explains WHY a node can't be wired. Returns the bare key so the
 * canvas can render the tooltip via `t(key)`.
 */
function unreachableReason(code: string): string {
  switch (code) {
    case 'incompatibleSource':
    case 'incompatibleTarget':
      return 'canvas.connectionMode.unreachable.type'
    case 'tooManyOutgoing':
    case 'tooManyIncoming':
      return 'canvas.connectionMode.unreachable.tooMany'
    case 'selfLoop':
      return 'canvas.connectionMode.unreachable.selfLoop'
    case 'duplicateEdge':
      return 'canvas.connectionMode.unreachable.duplicate'
    default:
      return 'canvas.connectionMode.unreachable.incompatible'
  }
}

function CanvasInner() {
  const {
    nodes,
    edges,
    addNode,
    addEdge,
    removeNode,
    removeEdge,
    duplicateNode,
    updateNodeData,
    selectNode,
  } = useWorkflowStore()
  const { fitView, screenToFlowPosition } = useReactFlow()

  const wrapperRef = useRef<HTMLDivElement>(null)
  const [menu, setMenu] = useState<{ x: number; y: number; kind: CanvasContextKind; id?: string } | null>(null)

  // The id of the node currently being dragged out of (when the user
  // is wiring a new edge). While set, every other node is colored
  // either "reachable" (normal) or "unreachable" (greyed out + tooltip).
  const [pendingSource, setPendingSource] = useState<string | null>(null)

  /**
   * Validates a candidate connection `src → tgt` for the live-drag UX.
   * Returns only errors that the candidate EDGE itself would cause
   * (`incompatibleSource` / `incompatibleTarget` / `selfLoop` /
   * `duplicateEdge` / `tooManyOutgoing` / `tooManyIncoming`). It does
   * NOT surface workflow-level problems like `missingInput` /
   * `missingOutgoing` — those describe the graph as a whole, not the
   * candidate, and would dim every node during a drag.
   *
   * : if the source is a tool-source node
   * (`tools` / `http` / `mcp`), the candidate is by definition a
   * `tool_attachment` edge — we validate against that rule table
   * instead of the dataflow one. Otherwise the drag is dataflow.
   *
   * Shared by the drop-time validator, the `isValidConnection`
   * callback, and the drag-state reachable set.
   */
  const validateConnection = useCallback(
    (src: string, tgt: string): RuleError[] => {
      if (!src || !tgt) return []
      const srcNode = nodes.find((n) => n.id === src)
      const kind: EdgeKind =
        srcNode && TOOL_SOURCE_TYPES.has(srcNode.type)
          ? 'tool_attachment'
          : 'dataflow'
      return wouldBeValidConnection(src, tgt, nodes, edges, kind)
    },
    [nodes, edges],
  )

  /**
   * For the current `pendingSource`, compute the set of node ids that
   * can legally be wired to. Re-runs whenever the graph or the source
   * changes. The map carries the first error code per id so the
   * canvas can show a specific tooltip.
   */
  const unreachable = useMemo(() => {
    if (!pendingSource) {
      // No drag in progress — nothing is "unreachable".
      return { reachable: new Set<string>(), reasons: new Map<string, string>() }
    }
    const reachable = new Set<string>()
    const reasons = new Map<string, string>()
    for (const n of nodes) {
      if (n.id === pendingSource) continue
      const errs = validateConnection(pendingSource, n.id)
      if (errs.length === 0) {
        reachable.add(n.id)
      } else {
        reasons.set(n.id, unreachableReason(errs[0].code))
      }
    }
    return { reachable, reasons }
  }, [pendingSource, nodes, validateConnection])

  // Local, React-Flow-controlled copy of the workflow's edges.
  //
  // Why a separate copy (mirrors the `rfNodes` pattern below):
  //   React Flow needs to mutate `selected` on edges when the user
  //   left-clicks one — and it then emits `{type: 'select', id,
  //   selected: true}` to `onEdgesChange`. Before the local-copy
  //   pattern, `rfEdges` was a pure `useMemo` over `edges.map(flowEdge)`,
  //   so the result of `applyEdgeChanges` was discarded and the next
  //   re-render rebuilt `rfEdges` from the store with every edge
  //   unselected. The user saw no highlight on click, concluded the
  //   edge couldn't be selected, and the canvas had no working
  //   left-click-to-select-and-delete UX (right-click context menu
  //   was the only path). The fix is the edge half of the rfNodes
  //   recipe: own a local copy, mutate via `applyEdgeChanges`, and
  //   re-seed from the store on every store change while preserving
  //   the `selected` flag for ids still in the store.
  //   `resyncRfEdges` (canvasNodes.ts) encodes the preservation rule.
  const [rfEdges, setRfEdges] = useState<Edge[]>(() => edges.map(flowEdge))

  useEffect(() => {
    setRfEdges((prev) => resyncRfEdges(prev, edges))
  }, [edges])

  /**
   * Local, React-Flow-controlled copy of the workflow's nodes.
   *
   * Why a separate copy:
   *   React Flow needs to mutate node position during drag (and
   *   it mutates other fields too — selection, dimensions) so it
   *   can render the drag preview. If we fed it the store-derived
   *   `nodes` directly AND forwarded position changes to the store
   *   on every mousemove, two problems emerged at 10+ nodes:
   *     (a) per-tick Zustand updates rebuilt `nodes` → rebuilt the
   *         `nodes.map(flowNode)` memo → new `onNodesChange`
   *         callback identity → React Flow tore down its internal
   *         store mid-drag, and the dragged node snapped to the
   *         release point instead of following the cursor;
   *     (b) React Flow emitted the
   *         "trying to drag a node that is not initialized"
   *         warning when its per-tick node objects didn't match
   *         what it had previously cached.
   *
   * The pattern (, after the  "perf + warning"
   * fix traded drag preview for stability):
   *   - `rfNodes` is owned by React Flow and mutated via
   *     `applyNodeChanges` in `onNodesChange` so the dragged node
   *     visually follows the cursor.
   *   - `onNodeDragStop` commits the final position to the store
   *     ONCE per drag.
   *   - A `useEffect` re-syncs from the store whenever the store's
   *     `nodes` reference changes (form edit, import, structural
   *     mutation), preserving any local position React Flow has
   *     optimistically set — so an in-flight drag isn't clobbered
   *     by a concurrent config edit on a different node.
   */
  const [rfNodes, setRfNodes] = useState<Node[]>(() => nodes.map(flowNode))

  /**
   * Ids of nodes the user is CURRENTLY dragging. Updated via React
   * Flow's drag-start / drag-stop callbacks. Passed to `resyncRfNodes`
   * so the useEffect that re-seeds `rfNodes` from the store knows
   * which positions to keep (the in-flight drags) vs which to take
   * from the store (everything else — paste, import, layout move).
   *
   * Why a ref and not state:
   *   Changing this must NOT trigger a re-render. It's only read
   *   inside the useEffect that watches `nodes`; reading from a ref
   *   there keeps the rule "re-render only when `nodes` changes"
   *   intact, which is what protects the per-tick perf at 10+ nodes.
   */
  const draggingIdsRef = useRef<Set<string>>(new Set())

  useEffect(() => {
    // Store changed — re-seed from store data. For nodes the user is
    // actively dragging, KEEP React Flow's local position so the drag
    // isn't torn back to the start point. For everything else, take
    // the store's position verbatim — so paste/import/layout moves
    // aren't clobbered.
    setRfNodes((prev) => resyncRfNodes(prev, nodes, draggingIdsRef.current))
  }, [nodes])

  const onNodesChange = useCallback(
    (changes: NodeChange[]) => {
      // Hand position/dimension/select changes to React Flow
      // locally — this is what makes the dragged node follow the
      // cursor. Only `remove` is propagated to the store: we don't
      // want a per-tick Zustand update mid-drag (see the rfNodes
      // comment for why).
      //
      // `select` changes are NOT propagated to the store either —
      // only an explicit left-click via `onNodeClick` should open
      // the property panel.
      setRfNodes((prev) => applyNodeChanges(changes, prev))
      for (const c of changes) {
        if (c.type === 'remove') {
          removeNode(c.id)
        }
      }
    },
    [removeNode],
  )

  /**
   * Wrap `rfNodes` with the unreachable-during-connection-drag
   * overlay. The overlay is computed off `rfNodes` (not the store)
   * so it sees the same position React Flow is animating.
   */
  const displayedNodes: Node[] = useMemo(() => {
    if (!pendingSource) return rfNodes
    return rfNodes.map((n) => {
      if (n.id === pendingSource) return n
      if (unreachable.reachable.has(n.id)) return n
      const reason =
        unreachable.reasons.get(n.id) ?? 'canvas.connectionMode.unreachable.incompatible'
      return {
        ...n,
        // `opacity-40` dims the node; `pointer-events-none` skips it
        // for React Flow's hit testing so the user can't drop on it.
        // The custom node wrapper also reads `data.unreachable` and
        // renders a `cursor-not-allowed` + tooltip.
        className: 'opacity-40 pointer-events-none',
        data: {
          ...n.data,
          unreachable: true,
          unreachableReason: i18n(reason),
        },
      }
    })
  }, [rfNodes, pendingSource, unreachable])

  /**
   * React Flow calls this once when the drag begins (mouse-down on
   * a node), before the per-tick position updates arrive. Cleared
   * in `onNodeDragStop`.
   */
  const onNodeDragStart = useCallback((_event: React.MouseEvent, node: Node) => {
    draggingIdsRef.current.add(node.id)
  }, [])

  /**
   * Commit the final position of a dragged node to the store.
   * React Flow handles the optimistic UI during the drag (no
   * per-tick re-renders of the whole canvas); this fires once when
   * the user releases. Reading via `useWorkflowStore.getState()`
   * keeps the callback stable — no `nodes` in deps means it never
   * re-creates, which would otherwise force React Flow to
   * re-instantiate and re-init its internal store on every drag.
   *
   * We drop the node from `draggingIdsRef` BEFORE committing so the
   * re-seed triggered by the commit takes the now-correct store
   * position for this node (which equals the local position we just
   * dragged to). If we cleared the ref after the commit, the in-flight
   * re-seed would still see this id as "preserve" and keep a stale
   * local position — visually identical in this case, but the
   * invariant "preserve = currently dragging" stays cleaner.
   */
  const onNodeDragStop = useCallback((_event: React.MouseEvent, node: Node) => {
    draggingIdsRef.current.delete(node.id)
    useWorkflowStore.setState((s) => ({
      nodes: s.nodes.map((x) =>
        x.id === node.id ? { ...x, position: { x: node.position.x, y: node.position.y } } : x,
      ),
      dirty: true,
    }))
  }, [])

  // Real left-click on a node → open the panel (only for configurable types).
  const onNodeClick = useCallback(
    (_event: React.MouseEvent, node: Node) => {
      const n = nodes.find((x) => x.id === node.id)
      if (n && isConfigurable(n.type)) selectNode(node.id)
    },
    [nodes, selectNode],
  )

  // Click on empty canvas → close the panel.
  const onPaneClick = useCallback(() => {
    selectNode(null)
  }, [selectNode])

  const onEdgesChange = useCallback(
    (changes: EdgeChange[]) => {
      // Apply selection / dimension / remove mutations to the local
      // copy so React Flow's internal state and the render tree agree
      // (this is what makes the highlight visible). Only `remove` is
      // propagated to the store — selection lives in the local copy,
      // not the store, so a per-change Zustand update would be
      // wasteful and would race concurrent store changes.
      setRfEdges((prev) => applyEdgeChanges(changes, prev))
      for (const c of changes) {
        if (c.type === 'remove') {
          removeEdge(c.id)
        }
      }
    },
    [removeEdge],
  )

  // Pre-flight check: if the proposed connection would violate any rule,
  // return false so React Flow turns the drag line red. Delegates to
  // the shared `validateConnection` helper so the drop-time check and
  // the drag-state "reachable" set use the same logic.
  const isValidConnection = useCallback(
    (conn: Connection | { source: string | null; target: string | null }): boolean => {
      if (!conn.source || !conn.target) return false
      return validateConnection(conn.source, conn.target).length === 0
    },
    [validateConnection],
  )

  // React Flow fires `onConnectStart` the moment the user picks up a
  // handle. We capture the source so the rest of the canvas can show
  // reachable vs unreachable states. Cleared on connect / cancel.
  const onConnectStart = useCallback(
    (_event: unknown, params: { nodeId: string | null; handleId: string | null; handleType: string | null }) => {
      setPendingSource(params.nodeId ?? null)
    },
    [],
  )

  // Surface a rejection message via the toolbar error bar. Triggered when
  // a user attempts a connection that `isValidConnection` rejects (drag
  // ended but no valid drop target). React Flow's `onConnectEnd` fires
  // whenever a drag finishes; we use the `to` field to decide whether a
  // drop actually happened.
  const onConnectEnd = useCallback(
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    (_event: MouseEvent | TouchEvent | null) => {
      // Drag ended — clear the "in progress" state so all nodes return
      // to their normal appearance. If the drop landed on a valid
      // handle, `onConnect` already fired; we just clean up state.
      setPendingSource(null)
    },
    [],
  )

  const onConnect = useCallback(
    (conn: Connection) => {
      setPendingSource(null)
      if (!conn.source || !conn.target) return
      if (conn.source === conn.target) return
      const src = conn.source
      const tgt = conn.target
      // Run the rule validator one more time so we can show a precise
      // message instead of silently dropping a bad edge.
      const ruleErrors = validateConnection(src, tgt)
      const first = ruleErrors[0]
      if (first) {
        useWorkflowStore.setState({
          error: i18n(`errors.connection.${first.code}`, first as unknown as Record<string, string | number>) || first.message,
        })
        return
      }
      // : tag the new edge with the correct kind
      // so the validator's per-kind dispatch (and the backend's IR
      // builder) pick the right rule table. Tool-source → agent drags
      // are `tool_attachment`; everything else stays `dataflow`.
      const srcNode = nodes.find((n) => n.id === src)
      const kind: EdgeKind =
        srcNode && TOOL_SOURCE_TYPES.has(srcNode.type)
          ? 'tool_attachment'
          : 'dataflow'
      addEdge({
        id: `e-${src}-${tgt}-${Date.now().toString(36)}`,
        source: src,
        target: tgt,
        sourceHandle: conn.sourceHandle ?? undefined,
        targetHandle: conn.targetHandle ?? undefined,
        kind,
      })
    },
    [addEdge, nodes, validateConnection],
  )

  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
  }, [])

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault()
      const raw = e.dataTransfer.getData('application/agnobuilder-node-type')
      // phase : validate against the manifest before
      // spawning the node. Drag payloads can be empty (user dragged
      // off-palette), corrupted (a stale browser extension intercepted
      // the drop), or names of node types that have since been
      // removed. Silently spawning an unknown node used to render an
      // unstyled rectangle on the canvas; now we reject the drop
      // outright so the rest of the workflow keeps working.
      if (!raw || !isKnownNodeType(raw)) {
        if (import.meta.env.DEV && raw) {
          // eslint-disable-next-line no-console
          console.warn(
            `[WorkflowCanvas] drop with unknown node type ${JSON.stringify(raw)}; ignoring.`,
          )
        }
        return
      }
      // React Flow stores node positions in its own internal coordinate
      // system (flow space), which differs from screen-space once the
      // user has panned or zoomed. `screenToFlowPosition` correctly
      // accounts for the current viewport transform so the dropped node
      // lands exactly under the cursor. We then subtract half the node
      // width so the cursor anchors at the horizontal center of the
      // new node instead of its top-left corner — matching what most
      // node editors (and the user) expect when they release a drag.
      const flow = screenToFlowPosition({ x: e.clientX, y: e.clientY })
      addNode(raw, {
        x: flow.x - NODE_WIDTH / 2,
        y: flow.y - NODE_HEADER_HEIGHT / 2,
      })
    },
    [addNode, screenToFlowPosition],
  )

  // ───── Right-click context menu ───────────────────────────────────
  const onPaneContextMenu = useCallback((event: React.MouseEvent | MouseEvent) => {
    event.preventDefault()
    setMenu({ x: event.clientX, y: event.clientY, kind: 'pane' })
  }, [])

  const onNodeContextMenu = useCallback(
    (event: React.MouseEvent, node: Node) => {
      event.preventDefault()
      // Stop the bubble: the outer wrapper also has onContextMenu for
      // the pane, and without this the pane handler fires AFTER ours
      // and overwrites the menu's `kind` from 'node' back to 'pane',
      // so the user sees "Add node…" instead of "Delete".
      event.stopPropagation()
      // Right-click also selects the node — opens the property panel
      // (for configurable types) and lights up the outline so the user
      // sees what the menu acts on. React Flow's left-click selection
      // model isn't triggered by the contextmenu event, so we drive
      // the highlight from the store. Non-configurable types (input /
      // output) also get highlighted so the user knows which node the
      // delete option would target.
      const n = nodes.find((x) => x.id === node.id)
      if (n) selectNode(node.id)
      setMenu({ x: event.clientX, y: event.clientY, kind: 'node', id: node.id })
    },
    [nodes, selectNode],
  )

  const onEdgeContextMenu = useCallback(
    (event: React.MouseEvent, edge: Edge) => {
      event.preventDefault()
      // See onNodeContextMenu — without stopPropagation, the pane
      // handler fires on bubble and overwrites our `kind: 'edge'`.
      event.stopPropagation()
      setMenu({ x: event.clientX, y: event.clientY, kind: 'edge', id: edge.id })
    },
    [],
  )

  const handleAddNode = useCallback(
    (type: NodeType) => {
      if (!menu) return
      // Same pan/zoom-aware conversion as `onDrop` so the right-click
      // "Add node…" menu places the new node exactly at the cursor.
      const flow = screenToFlowPosition({ x: menu.x, y: menu.y })
      addNode(type, {
        x: flow.x - NODE_WIDTH / 2,
        y: flow.y - NODE_HEADER_HEIGHT / 2,
      })
    },
    [addNode, menu, screenToFlowPosition],
  )

  const handleDelete = useCallback(() => {
    if (!menu) return
    if (menu.kind === 'node' && menu.id) removeNode(menu.id)
    else if (menu.kind === 'edge' && menu.id) removeEdge(menu.id)
  }, [menu, removeNode, removeEdge])

  const handleDuplicate = useCallback(() => {
    if (!menu || menu.kind !== 'node' || !menu.id) return
    duplicateNode(menu.id)
  }, [menu, duplicateNode])

  const handleFitView = useCallback(() => {
    fitView({ padding: 0.2, duration: 200 })
  }, [fitView])

  void updateNodeData

  return (
    <div className="flex flex-col h-full">
      {/* NodePalette sits flush above the canvas. Drag a chip
          down onto the drop target below to add a node — the
          wrapper div still owns the onDrop handler so the
          drop-to-position behaviour is unchanged. */}
      <NodePalette />
      <div
        ref={wrapperRef}
        className="flex-1 min-h-0 bg-canvas-bg"
        onDragOver={onDragOver}
        onDrop={onDrop}
        onContextMenu={onPaneContextMenu}
      >
        <ReactFlow
          nodes={displayedNodes}
          edges={rfEdges}
          nodeTypes={customNodeTypes}
          edgeTypes={customEdgeTypes}
          connectionLineComponent={ConnectionLine}
          onNodesChange={onNodesChange}
          onNodeDragStart={onNodeDragStart}
          onNodeDragStop={onNodeDragStop}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onConnectStart={onConnectStart}
          onConnectEnd={onConnectEnd}
          isValidConnection={isValidConnection}
          onNodeClick={onNodeClick}
          onPaneClick={onPaneClick}
          onNodeContextMenu={onNodeContextMenu}
          onEdgeContextMenu={onEdgeContextMenu}
          onPaneContextMenu={onPaneContextMenu}
          fitView
          deleteKeyCode={['Delete', 'Backspace']}
          proOptions={{ hideAttribution: true }}
        >
          <Background gap={16} color="rgb(var(--canvas-grid))" />
          <Controls className="[&>button]:bg-surface [&>button]:border-edge [&>button]:text-ink-muted [&>button:hover]:bg-surface-2" />
        </ReactFlow>
        {menu && (
          <CanvasContextMenu
            x={menu.x}
            y={menu.y}
            kind={menu.kind}
            onClose={() => setMenu(null)}
            onAddNode={handleAddNode}
            onDelete={handleDelete}
            onDuplicate={handleDuplicate}
            onFitView={handleFitView}
          />
        )}
      </div>
    </div>
  )
}

export function WorkflowCanvas() {
  return (
    <ReactFlowProvider>
      <CanvasInner />
    </ReactFlowProvider>
  )
}