/**
 * T29 (v41-gap-analysis #29) — pure dagre tier auto-layout for FixtureDesigner.
 *
 * Layout contract (doc §8.3): instruments top band → fixtures middle band → DUTs
 * bottom band; edges never dropped or reordered by layout; running the layout
 * twice is idempotent; empty input is a no-op.
 */
import { describe, expect, it } from 'vitest'
import {
  computeFixtureTierLayout,
  type LayoutInputEdge,
  type LayoutInputNode,
} from '../fixtureLayout'

function node(id: string, kind: LayoutInputNode['kind'], w = 160, h = 64): LayoutInputNode {
  return { id, kind, width: w, height: h }
}

/** Messy mixed graph: 3 instruments, 2 fixtures, 2 DUTs, forward/reverse/same-tier/unknown-id links. */
function messyGraph(): { nodes: LayoutInputNode[]; edges: LayoutInputEdge[] } {
  const nodes = [
    node('psu1', 'instrument'),
    node('dmm1', 'instrument'),
    node('eload1', 'instrument'),
    node('fix1', 'fixture'),
    node('fix2', 'fixture'),
    node('dut1', 'dut'),
    node('dut2', 'dut'),
  ]
  const edges: LayoutInputEdge[] = [
    { source: 'psu1', target: 'fix1' }, // forward
    { source: 'fix1', target: 'dut1' }, // forward
    { source: 'dut2', target: 'eload1' }, // reverse direction — must not break tiering
    { source: 'dmm1', target: 'dmm1x' }, // unknown endpoint — ignored
    { source: 'ghost', target: 'fix2' }, // unknown endpoint — ignored
  ]
  return { nodes, edges }
}

describe('computeFixtureTierLayout', () => {
  it('groups tiers vertically: all instruments above all fixtures above all DUTs', () => {
    const { positions } = computeFixtureTierLayout(messyGraph().nodes, messyGraph().edges)
    const maxYOf = (kind: LayoutInputNode['kind']) =>
      Math.max(...messyGraph().nodes.filter((n) => n.kind === kind).map((n) => positions[n.id].y))
    const minYOf = (kind: LayoutInputNode['kind']) =>
      Math.min(...messyGraph().nodes.filter((n) => n.kind === kind).map((n) => positions[n.id].y))
    expect(maxYOf('instrument')).toBeLessThan(minYOf('fixture'))
    expect(maxYOf('fixture')).toBeLessThan(minYOf('dut'))
  })

  it('positions every input node exactly once with finite coordinates', () => {
    const { nodes, edges } = messyGraph()
    const { positions } = computeFixtureTierLayout(nodes, edges)
    expect(Object.keys(positions).sort()).toEqual(nodes.map((n) => n.id).sort())
    for (const n of nodes) {
      expect(Number.isFinite(positions[n.id].x)).toBe(true)
      expect(Number.isFinite(positions[n.id].y)).toBe(true)
    }
  })

  it('does not overlap nodes within a tier row (respects width + nodeSep)', () => {
    const { nodes, edges } = messyGraph()
    const { positions } = computeFixtureTierLayout(nodes, edges, { nodeSep: 60 })
    for (const kind of ['instrument', 'fixture', 'dut'] as const) {
      const rows = nodes
        .filter((n) => n.kind === kind)
        .map((n) => ({ ...positions[n.id], w: n.width }))
        .sort((a, b) => a.x - b.x)
      for (let i = 1; i < rows.length; i++) {
        expect(rows[i].x).toBeGreaterThanOrEqual(rows[i - 1].x + rows[i - 1].w + 60)
      }
    }
  })

  it('is idempotent — a second identical call yields identical positions', () => {
    const { nodes, edges } = messyGraph()
    const first = computeFixtureTierLayout(nodes, edges)
    const second = computeFixtureTierLayout(nodes, edges)
    expect(second.positions).toEqual(first.positions)
  })

  it('keeps reverse-direction edges (DUT→instrument) from breaking tier grouping', () => {
    // Edge points strictly upward (dut → instrument); dagre must see it reversed,
    // so tiering stays intact and identical to the forward-edge variant.
    const nodes = [node('i1', 'instrument'), node('f1', 'fixture'), node('d1', 'dut')]
    const up = computeFixtureTierLayout(nodes, [{ source: 'd1', target: 'i1' }])
    const down = computeFixtureTierLayout(nodes, [{ source: 'i1', target: 'd1' }])
    expect(up.positions.i1.y).toBeLessThan(up.positions.f1.y)
    expect(up.positions.f1.y).toBeLessThan(up.positions.d1.y)
    expect(up.positions).toEqual(down.positions)
  })

  it('places isolated (edge-less) nodes inside their own tier band', () => {
    const nodes = [node('lonelyDut', 'dut'), node('lonelyInst', 'instrument')]
    const { positions } = computeFixtureTierLayout(nodes, [])
    expect(positions.lonelyInst.y).toBeLessThan(positions.lonelyDut.y)
  })

  it('returns an empty result for an empty graph (no-op, no throw)', () => {
    const { positions } = computeFixtureTierLayout([], [])
    expect(positions).toEqual({})
  })

  it('honours custom origin / rankSep options', () => {
    const nodes = [node('i1', 'instrument'), node('f1', 'fixture')]
    const { positions } = computeFixtureTierLayout(nodes, [{ source: 'i1', target: 'f1' }], {
      origin: { x: 100, y: 200 },
      rankSep: 120,
    })
    expect(positions.i1.x).toBe(100)
    expect(positions.i1.y).toBe(200)
    // fixture band starts at instrument height + rankSep
    expect(positions.f1.y).toBe(200 + 64 + 120)
  })
})
