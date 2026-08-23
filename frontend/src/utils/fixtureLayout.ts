/**
 * T29 (v41-gap-analysis #29) — pure dagre tier auto-layout for FixtureDesigner.
 *
 * Doc §8.3 topology convention: instruments on top, fixture in the middle,
 * DUTs at the bottom. This module is PURE (nodes+edges in → positions out):
 * no X6 / DOM imports, so it is unit-testable in isolation. The thin X6
 * adapter that reads a Graph and applies the computed positions lives in
 * `fixtureLayoutAdapter.ts`.
 *
 * Coordinate system: all positions are TOP-LEFT coordinates — exactly what
 * X6's `node.position(x, y)` expects — so the adapter never has to fight
 * X6's coordinate system or convert client/screen points.
 */
import dagre from 'dagre'

/** Entity tier of a FixtureDesigner canvas node (doc §8.3.2). */
export type FixtureNodeKind = 'instrument' | 'fixture' | 'dut'

/** Vertical band order: instruments top → fixtures middle → DUTs bottom. */
export const TIER_ORDER: readonly FixtureNodeKind[] = ['instrument', 'fixture', 'dut']

export interface LayoutInputNode {
  id: string
  kind: FixtureNodeKind
  width: number
  height: number
}

export interface LayoutInputEdge {
  source: string
  target: string
}

export interface TierLayoutOptions {
  /** Horizontal gap between nodes within one tier row. Default 60. */
  nodeSep?: number
  /** Vertical gap between tier bands. Default 80. */
  rankSep?: number
  /** Top-left origin of the laid-out block. Default { x: 40, y: 40 }. */
  origin?: { x: number; y: number }
}

export interface TierLayoutResult {
  /** id → top-left position, keyed by every input node id. */
  positions: Record<string, { x: number; y: number }>
}

/**
 * Compute tier-grouped positions for a fixture topology graph.
 *
 * Algorithm:
 *  1. Feed nodes + cross-tier edges into dagre (rankdir=TB). Same-tier edges
 *     are skipped and reverse edges (higher tier → lower tier) are reversed,
 *     so dagre's ranking always flows instrument→fixture→DUT. Dagre provides
 *     connectivity-aware horizontal ORDERING.
 *  2. Repack each tier as a single left-to-right row preserving dagre's x
 *     order (deterministic tie-break: input order), guaranteeing zero overlap.
 *  3. Stack tier bands vertically with fixed rankSep gaps — strict tier
 *     grouping regardless of edge directions or isolated nodes.
 *
 * Deterministic pure function of its inputs → invoking layout twice yields
 * identical positions (idempotent re-click).
 */
export function computeFixtureTierLayout(
  nodes: LayoutInputNode[],
  edges: LayoutInputEdge[],
  options: TierLayoutOptions = {},
): TierLayoutResult {
  const nodeSep = options.nodeSep ?? 60
  const rankSep = options.rankSep ?? 80
  const origin = options.origin ?? { x: 40, y: 40 }

  if (nodes.length === 0) {
    return { positions: {} }
  }

  // ── 1. dagre pass: connectivity-aware horizontal ordering ─────────────────
  const g = new dagre.graphlib.Graph()
  g.setGraph({ rankdir: 'TB', nodesep: nodeSep, ranksep: rankSep, marginx: 0, marginy: 0 })
  g.setDefaultEdgeLabel(() => ({}))

  const ids = new Set(nodes.map((n) => n.id))
  for (const n of nodes) {
    g.setNode(n.id, { width: n.width, height: n.height })
  }
  const tierOf = new Map(nodes.map((n) => [n.id, TIER_ORDER.indexOf(n.kind)]))
  for (const e of edges) {
    if (!ids.has(e.source) || !ids.has(e.target)) continue
    const fromTier = tierOf.get(e.source) as number
    const toTier = tierOf.get(e.target) as number
    if (fromTier === toTier) continue // same-tier links carry no rank info
    if (fromTier < toTier) g.setEdge(e.source, e.target)
    else g.setEdge(e.target, e.source) // reverse so ranks flow downward by tier
  }
  dagre.layout(g)

  // ── 2+3. repack rows per tier, stack bands vertically ─────────────────────
  const positions: Record<string, { x: number; y: number }> = {}
  const inputIndex = new Map(nodes.map((n, i) => [n.id, i]))
  let bandY = origin.y

  for (const kind of TIER_ORDER) {
    const tierNodes = nodes.filter((n) => n.kind === kind)
    if (tierNodes.length === 0) continue

    const ordered = [...tierNodes].sort((a, b) => {
      const xa = g.hasNode(a.id) ? (g.node(a.id).x as number) : 0
      const xb = g.hasNode(b.id) ? (g.node(b.id).x as number) : 0
      return xa - xb || (inputIndex.get(a.id) as number) - (inputIndex.get(b.id) as number)
    })

    let cursorX = origin.x
    for (const n of ordered) {
      positions[n.id] = { x: cursorX, y: bandY }
      cursorX += n.width + nodeSep
    }

    const bandHeight = Math.max(...tierNodes.map((n) => n.height))
    bandY += bandHeight + rankSep
  }

  return { positions }
}
