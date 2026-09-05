/**
 * X6 node breakpoint markers (task 23 — SequenceEditor breakpoint toggles).
 *
 * Step nodes in the SequenceEditor graph are plain X6 `Shape.Rect` cells
 * (registered in main.ts as `script-step-node`); their visuals are driven
 * via SVG attrs, NOT via the Vue ScriptStepNode.vue component (which is not
 * wired through x6-vue-shape in production). Breakpoint state therefore
 * renders as two extra SVG sub-elements on the node:
 *
 * - `bpBadge`  — a solid red dot pinned to the node's top-right corner,
 *                shown while an enabled `step` breakpoint targets the node.
 * - `bpHalo`   — an amber rounded ring around the node body while the
 *                node is the current BREAKPOINT_HIT target.
 *
 * The corresponding `<circle>` / `<rect>` markup elements are registered
 * ONCE on the shape in main.ts (`script-step-node`); these helpers only
 * flip their attrs (fill/stroke/display), so toggling never mutates the
 * markup tree and cannot disturb node layout.
 *
 * PURE helpers so they are unit-testable with plain fake node objects cast
 * to the X6 Node shape.
 */
import type { Node } from '@antv/x6'

/** Attr selector for the red breakpoint badge dot (top-right corner). */
export const BP_BADGE_SELECTOR = 'bpBadge'

/** Attr selector for the amber breakpoint-hit halo ring. */
export const BP_HALO_SELECTOR = 'bpHalo'

/** Badge geometry: dot radius in node-local coordinates. */
const BADGE_RADIUS = 6

/** Badge center offset from the node's top-right corner (node-local). */
const BADGE_OFFSET_X = 6
const BADGE_OFFSET_Y = 6

const BADGE_COLOR = '#ef4444' // red-500 — matches STATUS_VISUAL_MAP.failed stroke
const HALO_COLOR = '#f59e0b' // amber-500 — distinct from red (failure) / blue (running)
const HALO_STROKE_WIDTH = 3

/**
 * Minimal node surface the helpers touch (size + setAttrs). The real X6
 * Node satisfies it; test fakes implement just these members and are cast
 * to the X6 Node shape at call sites.
 */
export type MarkerNode = Pick<Node, 'size' | 'setAttrs'>

/**
 * Show/hide the red breakpoint badge on a node.
 *
 * The badge is positioned relative to the node's top-right corner; positions
 * are set on every show so a resized node keeps the badge pinned correctly.
 */
export function setBreakpointBadge(node: MarkerNode, active: boolean): void {
  if (!active) {
    node.setAttrs({ [BP_BADGE_SELECTOR]: { display: 'none' } })
    return
  }
  const { width } = node.size()
  node.setAttrs({
    [BP_BADGE_SELECTOR]: {
      display: 'block',
      refCx: width - BADGE_OFFSET_X,
      refCy: BADGE_OFFSET_Y,
      r: BADGE_RADIUS,
      fill: BADGE_COLOR,
      stroke: '#ffffff',
      strokeWidth: 1.5,
      style: { pointerEvents: 'none' },
    },
  })
}

/**
 * Show/hide the amber BREAKPOINT_HIT halo ring around a node.
 */
export function setBreakpointHitHalo(node: MarkerNode, active: boolean): void {
  if (!active) {
    node.setAttrs({ [BP_HALO_SELECTOR]: { display: 'none' } })
    return
  }
  const { width, height } = node.size()
  const margin = 5
  node.setAttrs({
    [BP_HALO_SELECTOR]: {
      display: 'block',
      x: -margin,
      y: -margin,
      width: width + margin * 2,
      height: height + margin * 2,
      rx: 16,
      ry: 16,
      fill: 'none',
      stroke: HALO_COLOR,
      strokeWidth: HALO_STROKE_WIDTH,
      style: { pointerEvents: 'none' },
    },
  })
}

/** Clear both breakpoint markers from every script-step node in a graph. */
export function clearAllBreakpointMarkers(graph: { getNodes: () => Node[] }): void {
  for (const node of graph.getNodes()) {
    setBreakpointBadge(node, false)
    setBreakpointHitHalo(node, false)
  }
}
