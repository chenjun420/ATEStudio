/**
 * T29 (v41-gap-analysis #29) — thin X6 adapter for the pure fixture tier layout.
 *
 * Reads nodes/edges from an AntV X6 Graph, delegates position math to the pure
 * `computeFixtureTierLayout`, and writes results back via `node.position()`.
 *
 * Coordinate-system note (spec: "must not fight X6's coordinate system"):
 * everything here stays in X6 LOCAL graph coordinates. `node.size()` and
 * `node.position()` both speak local coordinates, and we write back top-left
 * positions in that same space — no client/screen points are ever involved,
 * so no `graph.toLocalPoint` conversion is required.
 *
 * Metadata note: only positions are touched. Node data (`kind`, `entityId`,
 * name, status…), ports, and edges are never modified — links survive layout
 * untouched, and manual drags afterwards win because nothing re-layouts on
 * data change (layout runs only when the toolbar button is clicked).
 */
import type { Graph } from '@antv/x6'
import {
  computeFixtureTierLayout,
  type FixtureNodeKind,
  type LayoutInputNode,
} from './fixtureLayout'

/** Narrow a raw node-data kind string to a layout tier, or null if not a topology node. */
function tierOfData(kind: unknown): FixtureNodeKind | null {
  return kind === 'instrument' || kind === 'fixture' || kind === 'dut' ? kind : null
}

/**
 * Apply tier auto-layout to all topology nodes on the graph.
 *
 * @returns true if positions were applied; false when the graph is empty or
 * has no topology nodes (no-op).
 */
export function applyFixtureTierLayout(g: Graph): boolean {
  const x6Nodes = g.getNodes()
  const layoutNodes: LayoutInputNode[] = []
  for (const n of x6Nodes) {
    const d = (n.getData() ?? {}) as { kind?: string }
    const kind = tierOfData(d.kind)
    if (!kind) continue
    const size = n.size()
    layoutNodes.push({ id: n.id, kind, width: size.width, height: size.height })
  }
  if (layoutNodes.length === 0) return false

  const layoutEdges = g.getEdges().map((e) => ({
    source: e.getSourceCellId(),
    target: e.getTargetCellId(),
  }))

  const { positions } = computeFixtureTierLayout(layoutNodes, layoutEdges)

  for (const n of x6Nodes) {
    const p = positions[n.id]
    if (p) n.position(p.x, p.y)
  }
  return true
}
