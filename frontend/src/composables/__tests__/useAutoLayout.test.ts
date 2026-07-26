import { describe, it, expect } from 'vitest'
import { autoLayout } from '../useAutoLayout'
import type { GraphData } from '../useSerializer'
import type { ScriptStepData, LoopContainerData, VariableData } from '@/models/nodes/types'

/**
 * Helper to create a minimal ScriptStepData node config.
 */
function makeStep(id: string, x = 0, y = 0): { id: string; x: number; y: number; data: ScriptStepData } {
  return {
    id,
    x,
    y,
    data: {
      stepId: id,
      scriptName: 'test_script.py',
      scriptVersion: '',
      params: {},
      preconditions: [],
      resources: [],
      timeout: 60000,
      onFail: 'stop',
      exportOutputs: false,
      status: 'idle',
    },
  }
}

/**
 * Helper to make a LoopContainerData node config.
 */
function makeLoop(id: string, x = 0, y = 0): { id: string; x: number; y: number; data: LoopContainerData } {
  return {
    id,
    x,
    y,
    data: {
      loopId: id,
      loopType: 'for',
      condition: '',
      count: 3,
      executionMode: 'serial',
      maxConcurrency: 1,
      status: 'idle',
    },
  }
}

/**
 * Helper to make a VariableData node config.
 */
function makeVariable(id: string, x = 0, y = 0): { id: string; x: number; y: number; data: VariableData } {
  return {
    id,
    x,
    y,
    data: {
      variables: { foo: 'bar' },
    },
  }
}

describe('autoLayout', () => {
  it('returns empty array for empty graph', () => {
    const result = autoLayout({ nodes: [], edges: [], sequence: { name: '', version: '3.0', steps: [], max_concurrency: 4 } })
    expect(result.nodes).toEqual([])
    expect(result.edges).toEqual([])
  })

  it('positions a single node at origin area (0,0)', () => {
    const result = autoLayout({
      nodes: [makeStep('A')],
      edges: [],
      sequence: { name: '', version: '3.0', steps: [], max_concurrency: 4 },
    })
    expect(result.nodes).toHaveLength(1)
    // dagre centers the node, so with 240x80 node, x should be near 0
    const node = result.nodes[0]
    expect(node.x).toBeGreaterThanOrEqual(-240)
    expect(node.x).toBeLessThanOrEqual(240)
    expect(node.y).toBeGreaterThanOrEqual(-80)
    expect(node.y).toBeLessThanOrEqual(80)
  })

  describe('10-node chain (laid out in a line, not a grid)', () => {
    it('lays out a linear chain left to right', () => {
      const nodes = Array.from({ length: 10 }, (_, i) => makeStep(`step${i}`))
      const edges = []
      for (let i = 0; i < 9; i++) {
        edges.push({
          source: `step${i}`,
          target: `step${i + 1}`,
          data: { condition: { status: 'passed' } },
        })
      }
      const result = autoLayout({
        nodes,
        edges,
        sequence: { name: '', version: '3.0', steps: [], max_concurrency: 4 },
      })

      expect(result.nodes).toHaveLength(10)

      // All nodes should be in strictly increasing X order (left → right)
      const sortedByX = [...result.nodes].sort((a, b) => a.x - b.x)
      for (let i = 0; i < sortedByX.length - 1; i++) {
        // Each successive node should have a higher X (dagre ranks go left→right)
        expect(sortedByX[i + 1].x).toBeGreaterThan(sortedByX[i].x)
      }

      // Nodes in the same rank should have similar Y values
      // For a strict chain, all nodes should be in the same rank (nearly identical Y)
      const yValues = result.nodes.map(n => n.y)
      const yMin = Math.min(...yValues)
      const yMax = Math.max(...yValues)
      // A chain should have all nodes on the same rank line (small Y variance)
      expect(yMax - yMin).toBeLessThan(20)
    })
  })

  describe('10-node fan-out (A → B1...B9, no overlaps)', () => {
    it('lays out fan-out without overlapping positions', () => {
      const root = makeStep('root')
      const leaves = Array.from({ length: 9 }, (_, i) => makeStep(`leaf${i}`))
      const nodes = [root, ...leaves]
      const edges = leaves.map(leaf => ({
        source: 'root',
        target: leaf.id,
        data: { condition: { status: 'passed' } },
      }))
      const result = autoLayout({
        nodes,
        edges,
        sequence: { name: '', version: '3.0', steps: [], max_concurrency: 4 },
      })

      expect(result.nodes).toHaveLength(10)

      // Root node should be the leftmost (lowest X)
      const rootNode = result.nodes.find(n => n.id === 'root')!
      const leafNodes = result.nodes.filter(n => n.id.startsWith('leaf'))

      for (const leaf of leafNodes) {
        expect(leaf.x).toBeGreaterThan(rootNode.x)
      }

      // No two nodes should have the same position
      const positions = result.nodes.map(n => `${n.x},${n.y}`)
      const uniquePositions = new Set(positions)
      expect(uniquePositions.size).toBe(10)
    })
  })

  describe('3-level DAG (3 distinct columns)', () => {
    it('positions 3-level DAG with distinct X columns', () => {
      // Level 0: A
      // Level 1: B, C (both depend on A)
      // Level 2: D (depends on B and C)
      const nodes = [
        makeStep('A'),
        makeStep('B'),
        makeStep('C'),
        makeStep('D'),
      ]
      const edges = [
        { source: 'A', target: 'B', data: { condition: { status: 'passed' } } },
        { source: 'A', target: 'C', data: { condition: { status: 'passed' } } },
        { source: 'B', target: 'D', data: { condition: { status: 'passed' } } },
        { source: 'C', target: 'D', data: { condition: { status: 'passed' } } },
      ]
      const result = autoLayout({
        nodes,
        edges,
        sequence: { name: '', version: '3.0', steps: [], max_concurrency: 4 },
      })

      expect(result.nodes).toHaveLength(4)

      // Group nodes by approximate X position (rank)
      const tolerance = 50 // within same rank
      const ranks = new Map<number, string[]>()
      for (const node of result.nodes) {
        // Round X to nearest tolerance to group
        const rankKey = Math.round(node.x / tolerance) * tolerance
        const ids = ranks.get(rankKey) || []
        ids.push(node.id)
        ranks.set(rankKey, ids)
      }

      // Should have at least 3 distinct rank groups
      expect(ranks.size).toBeGreaterThanOrEqual(3)

      // A should be the leftmost
      const nodeA = result.nodes.find(n => n.id === 'A')!
      const nodeD = result.nodes.find(n => n.id === 'D')!
      expect(nodeA.x).toBeLessThan(nodeD.x)

      // B and C should be in the same rank (between A and D)
      const nodeB = result.nodes.find(n => n.id === 'B')!
      const nodeC = result.nodes.find(n => n.id === 'C')!
      expect(Math.abs(nodeB.x - nodeC.x)).toBeLessThan(tolerance)
    })
  })

  describe('disabled auto-layout', () => {
    it('returns original positions when enabled=false', () => {
      const nodes = [makeStep('A', 100, 200), makeStep('B', 300, 400)]
      const result = autoLayout(
        { nodes, edges: [], sequence: { name: '', version: '3.0', steps: [], max_concurrency: 4 } },
        { enabled: false },
      )
      expect(result.nodes[0].x).toBe(100)
      expect(result.nodes[0].y).toBe(200)
      expect(result.nodes[1].x).toBe(300)
      expect(result.nodes[1].y).toBe(400)
    })
  })

  describe('mixed node types', () => {
    it('handles script steps, loop containers, and variable nodes together', () => {
      const nodes = [
        makeStep('step1'),
        makeLoop('loop1'),
        makeVariable('var1'),
      ]
      const edges = [
        { source: 'step1', target: 'loop1', data: { condition: { status: 'passed' } } },
      ]
      const result = autoLayout({
        nodes,
        edges,
        sequence: { name: '', version: '3.0', steps: [], max_concurrency: 4 },
      })

      expect(result.nodes).toHaveLength(3)

      // All nodes should have valid finite positions
      for (const node of result.nodes) {
        expect(Number.isFinite(node.x)).toBe(true)
        expect(Number.isFinite(node.y)).toBe(true)
      }
    })
  })

  describe('child nodes are excluded from dagre', () => {
    it('child nodes (inside loop containers) keep their original positions', () => {
      // Simulate a graph with a loop container and a child node.
      // The child node has a non-zero position indicating it's inside a parent.
      // Child nodes are ignored by dagre — they keep their relative offsets.
      const parentLoop = makeLoop('loop1', 0, 0)
      const childNode = makeStep('child1', 20, 40) // positioned inside the loop container
      const nodes = [parentLoop, childNode]
      const edges = [
        { source: 'loop1', target: 'child1', data: { condition: { status: 'passed' } } }, // edge to child
      ]
      const result = autoLayout({
        nodes,
        edges,
        sequence: { name: '', version: '3.0', steps: [], max_concurrency: 4 },
      })

      expect(result.nodes).toHaveLength(2)

      // The child node should keep its original relative position (dagre skips it)
      const childResult = result.nodes.find(n => n.id === 'child1')!
      expect(childResult.x).toBe(20)
      expect(childResult.y).toBe(40)

      // Parent should have been moved from (0,0) by dagre
      const parentResult = result.nodes.find(n => n.id === 'loop1')!
      expect(parentResult.x).not.toBe(0)
      expect(parentResult.y).not.toBe(0)
    })
  })

  describe('custom graph config', () => {
    it('applies custom dagre graph label options', () => {
      const nodes = [makeStep('A'), makeStep('B')]
      const edges = [
        { source: 'A', target: 'B', data: { condition: { status: 'passed' } } },
      ]
      const resultDefault = autoLayout({
        nodes,
        edges,
        sequence: { name: '', version: '3.0', steps: [], max_concurrency: 4 },
      })
      const resultCustom = autoLayout(
        { nodes, edges, sequence: { name: '', version: '3.0', steps: [], max_concurrency: 4 } },
        { graphConfig: { nodesep: 500, ranksep: 300 } },
      )

      // With larger separation, the X distance should be greater
      const defaultDelta = Math.abs(resultDefault.nodes[1].x - resultDefault.nodes[0].x)
      const customDelta = Math.abs(resultCustom.nodes[1].x - resultCustom.nodes[0].x)
      expect(customDelta).toBeGreaterThan(defaultDelta)
    })
  })
})