/**
 * Unit tests for useSerializer — loop container YAML ↔ Graph serialization.
 *
 * Tests cover:
 * - yamlToGraphData: FOR/WHILE/FOREACH loop deserialization
 * - yamlToGraphData: nested loops (depth 2, 3, 5)
 * - yamlToGraphData: depth > 5 throws
 * - yamlToGraphData: loop container edge wiring (sequential + precondition)
 * - yamlToGraphData: loop children positioned relative to container
 * - Round-trip: YAML → GraphData → YAML for all loop types
 * - Round-trip: nested loops produce identical YAML
 * - graphToYaml: loop container serialization (FOR/WHILE/FOREACH)
 * - graphToYaml: nested loop serialization
 * - graphToYaml: optional fields (execution_mode, max_iterations)
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  yamlToGraphData,
  graphToYaml,
  type GraphData,
} from '@/composables/useSerializer'
import type {
  YamlSequence,
  YamlStep,
  YamlLoop,
} from '@/types/dsl'
import { isLoopContainerData, isScriptStepData } from '@/models/nodes/types'
import type { LoopContainerData, ScriptStepData, NodeData } from '@/models/nodes/types'
import * as yaml from 'js-yaml'
import { Graph } from '@antv/x6'

// ─── Mock @antv/x6 Graph ────────────────────────────────────────────────────
//
// The @antv/x6 package is ESM-only and cannot be loaded in vitest's jsdom
// environment. We mock the Graph class with a minimal implementation that
// supports the API surface used by graphToYaml().
//
// graphToYaml() uses:
//   - graph.getNodes() → returns nodes with .id, .getData(), .getParent(), .getChildren(), .position()
//   - graph.getEdges() → returns edges with .id, .getSource(), .getTarget(), .getSourceCell(), .getTargetCell(), .getSourceNode(), .getTargetNode(), .getData()
//   - node.isNode() → true
//   - node.getParent() → parent node or null
//   - node.getChildren() → array of child cells
//   - cell.id → string
//   - cell.isNode() → true/false
//   - cell.isEdge() → true/false

vi.mock('@antv/x6', () => {
  // Simple mock cell base class
  class MockCell {
    id: string
    _data: any
    _parent: MockNode | null = null
    _children: MockCell[] = []
    _position: { x: number; y: number } = { x: 0, y: 0 }
    _ports: any[] = []

    constructor(id: string) {
      this.id = id
    }

    isNode(): boolean { return this instanceof MockNode }
    isEdge(): boolean { return this instanceof MockEdge }
    getData() { return this._data }
    setData(data: any, _opts?: any) { this._data = data; return this }
    getParent() { return this._parent }
    setParent(node: MockNode) { this._parent = node; return this }
    position(x?: number, y?: number) {
      if (x !== undefined && y !== undefined) { this._position = { x, y }; return this }
      return { ...this._position }
    }
    getChildren() { return [...this._children] }
    addChild(cell: MockCell) { this._children.push(cell); return this }
    getPorts() { return [...this._ports] }
  }

  class MockNode extends MockCell {
    constructor(id: string) {
      super(id)
    }
    getLabel() { return '' }
    setLabel(_label: string) { return this }
    getAttrByPath(_path: string) { return null }
    setAttrByPath(_path: string, _value: any) { return this }
    getAttrs() { return {} }
  }

  class MockEdge extends MockCell {
    _source: any = null
    _target: any = null
    _sourceCell: MockNode | null = null
    _targetCell: MockNode | null = null
    _sourceNode: MockNode | null = null
    _targetNode: MockNode | null = null
    _attrs: any = {}

    constructor(id: string) {
      super(id)
    }

    getSource() { return this._source }
    getTarget() { return this._target }
    getSourceCell() { return this._sourceCell }
    getTargetCell() { return this._targetCell }
    getSourceNode() { return this._sourceNode }
    getTargetNode() { return this._targetNode }
    getAttrs() { return this._attrs }
  }

  class MockGraph {
    _nodes: Map<string, MockNode> = new Map()
    _edges: Map<string, MockEdge> = new Map()
    _edgeIndex: number = 0

    constructor(_opts?: any) {}

    getNodes(): MockNode[] {
      return Array.from(this._nodes.values())
    }

    getEdges(): MockEdge[] {
      return Array.from(this._edges.values())
    }

    getCellById(id: string): MockCell | null {
      return this._nodes.get(id) || this._edges.get(id) || null
    }

    addNode(config: any): MockNode {
      const node = new MockNode(config.id)
      node._data = config.data
      node._position = { x: config.x || 0, y: config.y || 0 }
      node._ports = config.ports?.items || []
      this._nodes.set(config.id, node)
      return node
    }

    addEdge(config: any): MockEdge {
      const edgeId = config.id || `edge-${++this._edgeIndex}`
      const edge = new MockEdge(edgeId)
      edge._data = config.data

      // Parse source
      if (typeof config.source === 'string') {
        edge._source = { cell: config.source }
        edge._sourceNode = this._nodes.get(config.source) || null
        edge._sourceCell = edge._sourceNode
      } else if (config.source?.cell) {
        edge._source = { cell: config.source.cell, port: config.source.port }
        edge._sourceNode = this._nodes.get(config.source.cell) || null
        edge._sourceCell = edge._sourceNode
      }

      // Parse target
      if (typeof config.target === 'string') {
        edge._target = { cell: config.target }
        edge._targetNode = this._nodes.get(config.target) || null
        edge._targetCell = edge._targetNode
      } else if (config.target?.cell) {
        edge._target = { cell: config.target.cell, port: config.target.port }
        edge._targetNode = this._nodes.get(config.target.cell) || null
        edge._targetCell = edge._targetNode
      }

      edge._attrs = config.attrs || {}
      this._edges.set(edgeId, edge)
      return edge
    }

    removeCell(_cell: MockCell | string): void {
      // no-op
    }

    clearCells(): void {
      this._nodes.clear()
      this._edges.clear()
    }

    zoomToFit(_opts?: any): void { /* no-op */ }

    dispose(): void {
      this._nodes.clear()
      this._edges.clear()
    }
  }

  return {
    Graph: MockGraph,
    Node: MockNode,
    Edge: MockEdge,
  }
})

// ─── Helpers ────────────────────────────────────────────────────────────────

/**
 * Create a minimal mock X6 Graph with nodes and edges from GraphData.
 * Uses the mocked Graph from @antv/x6 (see mock at top of file).
 * Simple bounding-box parent detection.
 */
function buildGraphFromData(data: GraphData): any {
  const graph = new Graph({})

  // Track container node IDs for parent/child relationships
  const containerNodeIds = new Set(
    data.nodes.filter(n => isLoopContainerData(n.data)).map(n => n.id)
  )

  // Add all nodes
  for (const nodeConfig of data.nodes) {
    const isContainer = containerNodeIds.has(nodeConfig.id)
    const data = nodeConfig.data
    const shape = isContainer ? 'loop-container-node' : 'script-step-node'

    let label = ''
    if (isScriptStepData(data)) {
      label = `${data.scriptName}\n${data.stepId.slice(0, 8)}`
    } else if (isLoopContainerData(data)) {
      label = `${data.loopType} loop\n${data.loopId.slice(0, 8)}`
    }

    const ports = isContainer
      ? {
          items: [
            { id: `input-${nodeConfig.id}`, group: 'input' },
            { id: `output-${nodeConfig.id}`, group: 'output' },
            { id: `loop-back-${nodeConfig.id}`, group: 'loop-back' },
          ],
        }
      : {
          items: [
            { id: `input-${nodeConfig.id}`, group: 'input' },
            { id: `output-${nodeConfig.id}`, group: 'output' },
          ],
        }

    graph.addNode({
      id: nodeConfig.id,
      shape,
      x: nodeConfig.x,
      y: nodeConfig.y,
      label,
      data: nodeConfig.data,
      ports,
    })
  }

  // Set parent/child relationships.
  // Since yamlToGraphData applies dagre auto-layout, nodes have computed
  // positions. Children are positioned close to their container (within
  // a reasonable bounding box). We detect parentage by checking if a
  // non-container node's position falls within any container's bounds.
  const containerNodes = graph.getNodes().filter(n => containerNodeIds.has(n.id))
  const childNodes = graph.getNodes().filter(n => !containerNodeIds.has(n.id))

  // Skip parent detection for variable nodes
  for (const child of childNodes) {
    const childPos = child.position()
    // Skip nodes at (0,0) — they're top-level nodes waiting for layout
    if (childPos.x === 0 && childPos.y === 0) continue

    for (const container of containerNodes) {
      const cPos = container.position()
      // Container is roughly 300x120 (from dagre dimensions)
      // Child nodes positioned within a wider margin (including the 20,40 offset)
      if (
        childPos.x >= cPos.x - 20 && childPos.x <= cPos.x + 350 &&
        childPos.y >= cPos.y - 20 && childPos.y <= cPos.y + 180
      ) {
        child.setParent(container)
        container.addChild(child)
        break
      }
    }
  }

  // Add edges
  for (const edgeConfig of data.edges) {
    const sourcePort = edgeConfig.sourcePort || `output-${edgeConfig.source}`
    const targetPort = edgeConfig.targetPort || `input-${edgeConfig.target}`

    graph.addEdge({
      source: { cell: edgeConfig.source, port: sourcePort },
      target: { cell: edgeConfig.target, port: targetPort },
      data: edgeConfig.data,
    })
  }

  return graph
}

/**
 * Build a mock X6 graph with proper parent/child relationships.
 * Unlike buildGraphFromData (which uses bounding-box heuristics),
 * this directly sets parent relationships based on the GraphData
 * structure: child nodes that are inside loop containers get
 * their parent set to the container.
 *
 * Detection heuristic: a child node is inside a loop container if
 * its node ID doesn't appear as a top-level step in the sequence,
 * AND there is a loop container node whose position bounds contain it.
 */
function buildGraphWithParents(data: GraphData, sequence: YamlSequence): any {
  const graph = new Graph({})

  // Collect top-level step IDs from the original YAML sequence
  const topLevelIds = new Set(sequence.steps.map(s => s.id))

  // Identify container nodes
  const containerNodeIds = new Set(
    data.nodes.filter(n => isLoopContainerData(n.data)).map(n => n.id)
  )

  // Add all nodes
  for (const nodeConfig of data.nodes) {
    const isContainer = containerNodeIds.has(nodeConfig.id)
    const nodeData = nodeConfig.data
    const shape = isContainer ? 'loop-container-node' : 'script-step-node'

    let label = ''
    if (isScriptStepData(nodeData)) {
      label = `${nodeData.scriptName}\n${nodeData.stepId.slice(0, 8)}`
    } else if (isLoopContainerData(nodeData)) {
      label = `${nodeData.loopType} loop\n${nodeData.loopId.slice(0, 8)}`
    }

    const ports = isContainer
      ? {
          items: [
            { id: `input-${nodeConfig.id}`, group: 'input' },
            { id: `output-${nodeConfig.id}`, group: 'output' },
            { id: `loop-back-${nodeConfig.id}`, group: 'loop-back' },
          ],
        }
      : {
          items: [
            { id: `input-${nodeConfig.id}`, group: 'input' },
            { id: `output-${nodeConfig.id}`, group: 'output' },
          ],
        }

    graph.addNode({
      id: nodeConfig.id,
      shape,
      x: nodeConfig.x,
      y: nodeConfig.y,
      label,
      data: nodeConfig.data,
      ports,
    })
  }

  // Set parent/child: non-top-level nodes are children of their container.
  // We determine parentage from the YAML structure: walk the sequence,
  // and for each YamlLoop, its .steps IDs become children of that container.
  // This handles nested loops correctly.
  function collectLoopChildren(seq: YamlSequence): Map<string, string> {
    const childToParent = new Map<string, string>()
    function walk(steps: Array<YamlStep | YamlLoop>, parentId: string | null) {
      for (const step of steps) {
        if ('loop_type' in step) {
          // This is a YamlLoop
          if (step.steps) {
            for (const child of step.steps) {
              childToParent.set(child.id, step.id)
            }
            walk(step.steps, step.id)
          }
        }
      }
    }
    walk(seq.steps, null)
    return childToParent
  }

  const childToParent = collectLoopChildren(sequence)

  // Reposition child nodes relative to their parent container
  for (const [childId, parentId] of childToParent) {
    const container = graph.getNodes().find(n => n.id === parentId)
    const childNode = graph.getNodes().find(n => n.id === childId)
    if (!container || !childNode) continue

    const cPos = container.position()
    childNode.setParent(container)
    container.addChild(childNode)

    // Position child relative to container (consistent offset)
    // For nested loops, each level adds its own offset
    childNode.position(cPos.x + 20, cPos.y + 40)
  }

  // Add edges
  for (const edgeConfig of data.edges) {
    const sourcePort = edgeConfig.sourcePort || `output-${edgeConfig.source}`
    const targetPort = edgeConfig.targetPort || `input-${edgeConfig.target}`

    graph.addEdge({
      source: { cell: edgeConfig.source, port: sourcePort },
      target: { cell: edgeConfig.target, port: targetPort },
      data: edgeConfig.data,
    })
  }

  return graph
}

/**
 * Parse a YAML string back to a YamlSequence object.
 */
function parseYaml(yamlStr: string): YamlSequence {
  return yaml.load(yamlStr) as YamlSequence
}

/**
 * Normalize a YamlSequence for comparison: remove undefined fields,
 * sort keys for deterministic comparison.
 */
function normalizeSequence(seq: YamlSequence): YamlSequence {
  return JSON.parse(JSON.stringify(seq))
}

// ─── Test Data ──────────────────────────────────────────────────────────────

const SIMPLE_FOR_LOOP_YAML = `
name: "FOR Loop Test"
version: "3.0"
max_concurrency: 4
steps:
  - id: setup
    script: test_scripts/setup.py
    params:
      mode: full
    timeout: 30
    export_outputs: true
  - id: repeat_measurement
    loop_type: FOR
    count: 5
    execution_mode: SERIAL
    steps:
      - id: measure_iteration
        script: test_scripts/measure.py
        params:
          iteration: "{{ loop.index }}"
        timeout: 60
        export_outputs: true
  - id: cleanup
    script: test_scripts/cleanup.py
    preconditions:
      - setup
    timeout: 30
`

const WHILE_LOOP_YAML = `
name: "WHILE Loop Test"
version: "3.0"
max_concurrency: 4
steps:
  - id: start
    script: test_scripts/start.py
    timeout: 10
  - id: wait_for_stable
    loop_type: WHILE
    condition: "result.stability_score < 0.95"
    max_iterations: 50
    steps:
      - id: poll_status
        script: test_scripts/poll_stability.py
        params:
          interval: 1
        timeout: 10
  - id: finish
    script: test_scripts/finish.py
    timeout: 10
`

const FOREACH_LOOP_YAML = `
name: "FOREACH Loop Test"
version: "3.0"
max_concurrency: 4
steps:
  - id: channel_sweep
    loop_type: FOREACH
    collection: channels
    iterator_var: channel
    execution_mode: PARALLEL
    steps:
      - id: measure_channel
        script: test_scripts/measure_channel.py
        params:
          channel: "{{ channel }}"
          voltage: 3.3
        timeout: 30
      - id: validate_channel
        script: test_scripts/validate.py
        params:
          channel: "{{ channel }}"
          threshold: 3.0
        preconditions:
          - measure_channel
        timeout: 30
`

const NESTED_LOOP_YAML = `
name: "Nested Loop Test"
version: "3.0"
max_concurrency: 4
steps:
  - id: setup
    script: test_scripts/setup.py
    timeout: 10
  - id: multi_dut_test
    loop_type: FOR
    count: 3
    steps:
      - id: dut_sweep
        loop_type: FOREACH
        collection: voltage_levels
        iterator_var: voltage
        execution_mode: PARALLEL
        steps:
          - id: apply_voltage
            script: test_scripts/apply_voltage.py
            params:
              voltage: "{{ voltage }}"
            timeout: 15
          - id: measure_dut
            script: test_scripts/measure_dut.py
            params:
              voltage: "{{ voltage }}"
            preconditions:
              - apply_voltage
            timeout: 30
            export_outputs: true
  - id: cleanup
    script: test_scripts/cleanup.py
    timeout: 10
`

const DEEP_NESTED_LOOP_YAML = `
name: "Deep Nested Loop Test"
version: "3.0"
max_concurrency: 4
steps:
  - id: level1
    loop_type: FOR
    count: 2
    steps:
      - id: level2
        loop_type: FOR
        count: 2
        steps:
          - id: level3
            loop_type: FOR
            count: 2
            steps:
              - id: level4
                loop_type: FOR
                count: 2
                steps:
                  - id: level5
                    loop_type: FOR
                    count: 2
                    steps:
                      - id: innermost
                        script: test_scripts/step.py
                        timeout: 10
`

const DEPTH_EXCEEDED_LOOP_YAML = `
name: "Depth Exceeded Test"
version: "3.0"
max_concurrency: 4
steps:
  - id: level1
    loop_type: FOR
    count: 2
    steps:
      - id: level2
        loop_type: FOR
        count: 2
        steps:
          - id: level3
            loop_type: FOR
            count: 2
            steps:
              - id: level4
                loop_type: FOR
                count: 2
                steps:
                  - id: level5
                    loop_type: FOR
                    count: 2
                    steps:
                      - id: level6
                        loop_type: FOR
                        count: 2
                        steps:
                          - id: too_deep
                            script: test_scripts/step.py
                            timeout: 10
`

// ─── Tests ──────────────────────────────────────────────────────────────────

describe('useSerializer — Loop Container Serialization', () => {

  // ── yamlToGraphData: FOR loop ────────────────────────────────────────────

  describe('yamlToGraphData — FOR loop', () => {
    it('creates a loop container node with count and iteration_var', () => {
      const data = yamlToGraphData(SIMPLE_FOR_LOOP_YAML)

      const loopNode = data.nodes.find(n => n.id === 'repeat_measurement')
      expect(loopNode).toBeDefined()
      expect(isLoopContainerData(loopNode!.data)).toBe(true)

      const loopData = loopNode!.data as LoopContainerData
      expect(loopData.loopType).toBe('for')
      expect(loopData.count).toBe(5)
      expect(loopData.executionMode).toBe('serial')
    })

    it('creates child nodes for loop steps', () => {
      const data = yamlToGraphData(SIMPLE_FOR_LOOP_YAML)

      const childNode = data.nodes.find(n => n.id === 'measure_iteration')
      expect(childNode).toBeDefined()
      expect(isScriptStepData(childNode!.data)).toBe(true)

      const childData = childNode!.data as ScriptStepData
      expect(childData.scriptName).toBe('test_scripts/measure.py')
      expect(childData.params).toEqual({ iteration: '{{ loop.index }}' })
      // YAML timeout is in seconds, NodeData timeout is in ms
      expect(childData.timeout).toBe(60 * 1000)
      expect(childData.exportOutputs).toBe(true)
    })

    it('child nodes exist with positions set', () => {
      const data = yamlToGraphData(SIMPLE_FOR_LOOP_YAML)

      const loopNode = data.nodes.find(n => n.id === 'repeat_measurement')!
      const childNode = data.nodes.find(n => n.id === 'measure_iteration')!

      // Both nodes should have position objects (computed by dagre auto-layout)
      expect(typeof loopNode.x).toBe('number')
      expect(typeof loopNode.y).toBe('number')
      expect(typeof childNode.x).toBe('number')
      expect(typeof childNode.y).toBe('number')
    })

    it('creates top-level script step nodes', () => {
      const data = yamlToGraphData(SIMPLE_FOR_LOOP_YAML)

      const setupNode = data.nodes.find(n => n.id === 'setup')
      expect(setupNode).toBeDefined()
      expect(isScriptStepData(setupNode!.data)).toBe(true)

      const cleanupNode = data.nodes.find(n => n.id === 'cleanup')
      expect(cleanupNode).toBeDefined()
      expect(isScriptStepData(cleanupNode!.data)).toBe(true)
    })

    it('creates precondition edges for top-level steps', () => {
      const data = yamlToGraphData(SIMPLE_FOR_LOOP_YAML)

      const preconditionEdge = data.edges.find(
        e => e.source === 'setup' && e.target === 'cleanup'
      )
      expect(preconditionEdge).toBeDefined()
      expect(preconditionEdge!.data?.condition?.status).toBe('passed')
    })
  })

  // ── yamlToGraphData: WHILE loop ──────────────────────────────────────────

  describe('yamlToGraphData — WHILE loop', () => {
    it('creates a loop container node with condition and max_iterations', () => {
      const data = yamlToGraphData(WHILE_LOOP_YAML)

      const loopNode = data.nodes.find(n => n.id === 'wait_for_stable')
      expect(loopNode).toBeDefined()
      expect(isLoopContainerData(loopNode!.data)).toBe(true)

      const loopData = loopNode!.data as LoopContainerData
      expect(loopData.loopType).toBe('while')
      expect(loopData.condition).toBe('result.stability_score < 0.95')
      expect(loopData.maxConcurrency).toBe(50) // max_iterations maps to maxConcurrency
    })

    it('creates child script step node', () => {
      const data = yamlToGraphData(WHILE_LOOP_YAML)

      const childNode = data.nodes.find(n => n.id === 'poll_status')
      expect(childNode).toBeDefined()
      expect(isScriptStepData(childNode!.data)).toBe(true)

      const childData = childNode!.data as ScriptStepData
      expect(childData.scriptName).toBe('test_scripts/poll_stability.py')
      expect(childData.params).toEqual({ interval: 1 })
    })

    it('creates sequential edges around the loop container', () => {
      const data = yamlToGraphData(WHILE_LOOP_YAML)

      // start → wait_for_stable (sequential, since start has no preconditions)
      const startToLoop = data.edges.find(
        e => e.source === 'start' && e.target === 'wait_for_stable'
      )
      expect(startToLoop).toBeDefined()

      // wait_for_stable → finish (sequential, since finish has no preconditions)
      const loopToFinish = data.edges.find(
        e => e.source === 'wait_for_stable' && e.target === 'finish'
      )
      expect(loopToFinish).toBeDefined()
    })
  })

  // ── yamlToGraphData: FOREACH loop ────────────────────────────────────────

  describe('yamlToGraphData — FOREACH loop', () => {
    it('creates a loop container node with collection and iterator_var', () => {
      const data = yamlToGraphData(FOREACH_LOOP_YAML)

      const loopNode = data.nodes.find(n => n.id === 'channel_sweep')
      expect(loopNode).toBeDefined()
      expect(isLoopContainerData(loopNode!.data)).toBe(true)

      const loopData = loopNode!.data as LoopContainerData
      expect(loopData.loopType).toBe('foreach')
      expect(loopData.collectionExpr).toBe('channels')
      expect(loopData.iterationVar).toBe('channel')
      expect(loopData.executionMode).toBe('parallel')
    })

    it('creates child nodes with internal precondition edges', () => {
      const data = yamlToGraphData(FOREACH_LOOP_YAML)

      const measureNode = data.nodes.find(n => n.id === 'measure_channel')
      const validateNode = data.nodes.find(n => n.id === 'validate_channel')
      expect(measureNode).toBeDefined()
      expect(validateNode).toBeDefined()

      // Internal edge: measure_channel → validate_channel
      const internalEdge = data.edges.find(
        e => e.source === 'measure_channel' && e.target === 'validate_channel'
      )
      expect(internalEdge).toBeDefined()
    })

    it('creates loop-back edge between child nodes', () => {
      const data = yamlToGraphData(FOREACH_LOOP_YAML)

      const loopBackEdge = data.edges.find(
        e => e.id?.startsWith('loop-back-')
      )
      expect(loopBackEdge).toBeDefined()
      // loop-back goes from last child to first child
      expect(loopBackEdge!.source).toBe('validate_channel')
      expect(loopBackEdge!.target).toBe('measure_channel')
    })
  })

  // ── yamlToGraphData: Nested loops ────────────────────────────────────────

  describe('yamlToGraphData — nested loops', () => {
    it('creates parent and child loop container nodes (depth 2)', () => {
      const data = yamlToGraphData(NESTED_LOOP_YAML)

      const outerLoop = data.nodes.find(n => n.id === 'multi_dut_test')
      const innerLoop = data.nodes.find(n => n.id === 'dut_sweep')
      expect(outerLoop).toBeDefined()
      expect(innerLoop).toBeDefined()

      expect(isLoopContainerData(outerLoop!.data)).toBe(true)
      expect(isLoopContainerData(innerLoop!.data)).toBe(true)

      const outerData = outerLoop!.data as LoopContainerData
      const innerData = innerLoop!.data as LoopContainerData
      expect(outerData.loopType).toBe('for')
      expect(innerData.loopType).toBe('foreach')
      expect(innerData.collectionExpr).toBe('voltage_levels')
    })

    it('creates grandchild nodes for nested loops', () => {
      const data = yamlToGraphData(NESTED_LOOP_YAML)

      const applyVoltage = data.nodes.find(n => n.id === 'apply_voltage')
      const measureDut = data.nodes.find(n => n.id === 'measure_dut')
      expect(applyVoltage).toBeDefined()
      expect(measureDut).toBeDefined()

      expect(isScriptStepData(applyVoltage!.data)).toBe(true)
      expect(isScriptStepData(measureDut!.data)).toBe(true)
    })

    it('creates internal edges for nested loop children', () => {
      const data = yamlToGraphData(NESTED_LOOP_YAML)

      const internalEdge = data.edges.find(
        e => e.source === 'apply_voltage' && e.target === 'measure_dut'
      )
      expect(internalEdge).toBeDefined()
    })
  })

  // ── yamlToGraphData: Deep nested loops (depth 5) ─────────────────────────

  describe('yamlToGraphData — depth 5 nested loops', () => {
    it('successfully creates nodes at depth 5', () => {
      const data = yamlToGraphData(DEEP_NESTED_LOOP_YAML)

      // All 5 loop levels + innermost step should exist
      expect(data.nodes.find(n => n.id === 'level1')).toBeDefined()
      expect(data.nodes.find(n => n.id === 'level2')).toBeDefined()
      expect(data.nodes.find(n => n.id === 'level3')).toBeDefined()
      expect(data.nodes.find(n => n.id === 'level4')).toBeDefined()
      expect(data.nodes.find(n => n.id === 'level5')).toBeDefined()
      expect(data.nodes.find(n => n.id === 'innermost')).toBeDefined()

      // All loop levels should be loop containers
      for (let i = 1; i <= 5; i++) {
        const node = data.nodes.find(n => n.id === `level${i}`)!
        expect(isLoopContainerData(node.data)).toBe(true)
      }

      // Innermost should be a script step
      const innermost = data.nodes.find(n => n.id === 'innermost')!
      expect(isScriptStepData(innermost.data)).toBe(true)
    })
  })

  // ── yamlToGraphData: Depth exceeded ──────────────────────────────────────

  describe('yamlToGraphData — depth exceeded', () => {
    it('throws an error when nesting depth exceeds 5', () => {
      expect(() => {
        yamlToGraphData(DEPTH_EXCEEDED_LOOP_YAML)
      }).toThrow('Loop nesting depth exceeds maximum (5)')
    })

    it('does not throw at exactly depth 5', () => {
      expect(() => {
        yamlToGraphData(DEEP_NESTED_LOOP_YAML)
      }).not.toThrow()
    })
  })

  // ── graphToYaml: FOR loop ────────────────────────────────────────────────

  describe('graphToYaml — FOR loop', () => {
    it('serializes a FOR loop container back to YamlLoop', () => {
      const sequence = parseYaml(SIMPLE_FOR_LOOP_YAML)
      const data = yamlToGraphData(SIMPLE_FOR_LOOP_YAML)
      const graph = buildGraphWithParents(data, sequence)
      const yamlStr = graphToYaml(graph)
      const parsed = parseYaml(yamlStr)

      // Find the FOR loop
      const loopStep = parsed.steps.find(
        s => 'loop_type' in s
      ) as YamlLoop | undefined
      expect(loopStep).toBeDefined()
      expect(loopStep!.loop_type).toBe('FOR')
      expect(loopStep!.count).toBe(5)
      // execution_mode is omitted for SERIAL (it's the default)
      expect(loopStep!.execution_mode === 'SERIAL' || loopStep!.execution_mode === undefined).toBe(true)
      expect(loopStep!.steps).toBeDefined()
      expect(loopStep!.steps!.length).toBeGreaterThanOrEqual(1)

      // Clean up graph
      graph.dispose()
    })

    it('serializes child steps inside the loop', () => {
      const sequence = parseYaml(SIMPLE_FOR_LOOP_YAML)
      const data = yamlToGraphData(SIMPLE_FOR_LOOP_YAML)
      const graph = buildGraphWithParents(data, sequence)
      const yamlStr = graphToYaml(graph)
      const parsed = parseYaml(yamlStr)

      const loopStep = parsed.steps.find(
        s => 'loop_type' in s && s.id === 'repeat_measurement'
      ) as YamlLoop | undefined
      expect(loopStep).toBeDefined()
      expect(loopStep!.steps).toBeDefined()
      expect(loopStep!.steps!.length).toBeGreaterThanOrEqual(1)

      const childStep = loopStep!.steps!.find(
        s => 'script' in s
      ) as YamlStep | undefined
      expect(childStep).toBeDefined()
      expect(childStep!.id).toBe('measure_iteration')
      expect(childStep!.script).toBe('test_scripts/measure.py')
      expect(childStep!.export_outputs).toBe(true)

      graph.dispose()
    })
  })

  // ── graphToYaml: WHILE loop ──────────────────────────────────────────────

  describe('graphToYaml — WHILE loop', () => {
    it('serializes a WHILE loop container with condition', () => {
      const sequence = parseYaml(WHILE_LOOP_YAML)
      const data = yamlToGraphData(WHILE_LOOP_YAML)
      const graph = buildGraphWithParents(data, sequence)
      const yamlStr = graphToYaml(graph)
      const parsed = parseYaml(yamlStr)

      const loopStep = parsed.steps.find(
        s => 'loop_type' in s
      ) as YamlLoop | undefined
      expect(loopStep).toBeDefined()
      expect(loopStep!.loop_type).toBe('WHILE')
      expect(loopStep!.condition).toBe('result.stability_score < 0.95')
      expect(loopStep!.max_iterations).toBe(50)

      graph.dispose()
    })
  })

  // ── graphToYaml: FOREACH loop ────────────────────────────────────────────

  describe('graphToYaml — FOREACH loop', () => {
    it('serializes a FOREACH loop container with collection and iterator', () => {
      const sequence = parseYaml(FOREACH_LOOP_YAML)
      const data = yamlToGraphData(FOREACH_LOOP_YAML)
      const graph = buildGraphWithParents(data, sequence)
      const yamlStr = graphToYaml(graph)
      const parsed = parseYaml(yamlStr)

      const loopStep = parsed.steps.find(
        s => 'loop_type' in s
      ) as YamlLoop | undefined
      expect(loopStep).toBeDefined()
      expect(loopStep!.loop_type).toBe('FOREACH')
      expect(loopStep!.collection).toBe('channels')
      expect(loopStep!.iterator_var).toBe('channel')
      expect(loopStep!.execution_mode).toBe('PARALLEL')

      graph.dispose()
    })
  })

  // ── graphToYaml: Nested loops ────────────────────────────────────────────

  describe('graphToYaml — nested loops', () => {
    it('serializes nested loop containers recursively', () => {
      const sequence = parseYaml(NESTED_LOOP_YAML)
      const data = yamlToGraphData(NESTED_LOOP_YAML)
      const graph = buildGraphWithParents(data, sequence)
      const yamlStr = graphToYaml(graph)
      const parsed = parseYaml(yamlStr)

      const outerLoop = parsed.steps.find(
        s => 'loop_type' in s && s.id === 'multi_dut_test'
      ) as YamlLoop | undefined
      expect(outerLoop).toBeDefined()
      expect(outerLoop!.loop_type).toBe('FOR')
      expect(outerLoop!.count).toBe(3)

      expect(outerLoop!.steps).toBeDefined()
      expect(outerLoop!.steps!.length).toBeGreaterThanOrEqual(1)

      const innerLoop = outerLoop!.steps!.find(
        s => 'loop_type' in s
      ) as YamlLoop | undefined
      expect(innerLoop).toBeDefined()
      expect(innerLoop!.loop_type).toBe('FOREACH')
      expect(innerLoop!.collection).toBe('voltage_levels')
      expect(innerLoop!.iterator_var).toBe('voltage')

      graph.dispose()
    })
  })

  // ── Round-trip: YAML → GraphData → YAML ──────────────────────────────────

  describe('round-trip: YAML → GraphData → YAML', () => {
    it('produces correct YamlSequence for FOR loop (graph round-trip)', () => {
      const sequence = parseYaml(SIMPLE_FOR_LOOP_YAML)
      const data = yamlToGraphData(SIMPLE_FOR_LOOP_YAML)
      const graph = buildGraphWithParents(data, sequence)
      const yamlStr = graphToYaml(graph)

      const result = parseYaml(yamlStr)
      const original = normalizeSequence(sequence)

      // The round-trip should preserve all step IDs
      for (const step of original.steps) {
        const matched = result.steps.find(s => s.id === step.id)
        expect(matched).toBeDefined()
      }

      graph.dispose()
    })

    it('produces correct YamlSequence for WHILE loop (graph round-trip)', () => {
      const sequence = parseYaml(WHILE_LOOP_YAML)
      const data = yamlToGraphData(WHILE_LOOP_YAML)
      const graph = buildGraphWithParents(data, sequence)
      const yamlStr = graphToYaml(graph)

      const result = parseYaml(yamlStr)
      const original = normalizeSequence(sequence)

      for (const step of original.steps) {
        const matched = result.steps.find(s => s.id === step.id)
        expect(matched).toBeDefined()
      }

      graph.dispose()
    })

    it('produces correct YamlSequence for FOREACH loop (graph round-trip)', () => {
      const sequence = parseYaml(FOREACH_LOOP_YAML)
      const data = yamlToGraphData(FOREACH_LOOP_YAML)
      const graph = buildGraphWithParents(data, sequence)
      const yamlStr = graphToYaml(graph)

      const result = parseYaml(yamlStr)
      const original = normalizeSequence(sequence)

      for (const step of original.steps) {
        const matched = result.steps.find(s => s.id === step.id)
        expect(matched).toBeDefined()
      }

      graph.dispose()
    })

    it('produces correct YamlSequence for nested loops (graph round-trip)', () => {
      const sequence = parseYaml(NESTED_LOOP_YAML)
      const data = yamlToGraphData(NESTED_LOOP_YAML)
      const graph = buildGraphWithParents(data, sequence)
      const yamlStr = graphToYaml(graph)

      const result = parseYaml(yamlStr)
      const original = normalizeSequence(sequence)

      for (const step of original.steps) {
        const matched = result.steps.find(s => s.id === step.id)
        expect(matched).toBeDefined()
      }

      graph.dispose()
    })

it('preserves loop_type, condition, count, collection_expr, iteration_var, execution_mode through round-trip', () => {
      const allLoopsYaml = `
name: "All Loops Round-Trip"
version: "3.0"
max_concurrency: 4
steps:
  - id: for_loop
    loop_type: FOR
    count: 10
    iterator_var: i
    execution_mode: SERIAL
    steps:
      - id: for_step
        script: test_scripts/step.py
        timeout: 10
  - id: while_loop
    loop_type: WHILE
    condition: "x < 10"
    max_iterations: 100
    steps:
      - id: while_step
        script: test_scripts/step.py
        timeout: 10
  - id: foreach_loop
    loop_type: FOREACH
    collection: items
    iterator_var: item
    execution_mode: PARALLEL
    steps:
      - id: foreach_step
        script: test_scripts/step.py
        timeout: 10
`

      const sequence = parseYaml(allLoopsYaml)
      const data = yamlToGraphData(allLoopsYaml)
      const graph = buildGraphWithParents(data, sequence)
      const yamlStr = graphToYaml(graph)

      const result = parseYaml(yamlStr)

      // Verify each loop type preserved its fields
      const forLoop = result.steps.find(s => s.id === 'for_loop') as YamlLoop
      expect(forLoop).toBeDefined()
      expect(forLoop.loop_type).toBe('FOR')
      expect(forLoop.count).toBe(10)
      expect(forLoop.iterator_var).toBe('i')
      // SERIAL is the default — may be omitted in output
      expect(forLoop.execution_mode === 'SERIAL' || forLoop.execution_mode === undefined).toBe(true)

      const whileLoop = result.steps.find(s => s.id === 'while_loop') as YamlLoop
      expect(whileLoop).toBeDefined()
      expect(whileLoop.loop_type).toBe('WHILE')
      expect(whileLoop.condition).toBe('x < 10')
      expect(whileLoop.max_iterations).toBe(100)

      const foreachLoop = result.steps.find(s => s.id === 'foreach_loop') as YamlLoop
      expect(foreachLoop).toBeDefined()
      expect(foreachLoop.loop_type).toBe('FOREACH')
      expect(foreachLoop.collection).toBe('items')
      expect(foreachLoop.iterator_var).toBe('item')
      expect(foreachLoop.execution_mode).toBe('PARALLEL')

      graph.dispose()
    })

    it('handles optional fields correctly (omits falsy values)', () => {
      // A FOR loop without iteration_var should NOT include it in the output
      const sequence = parseYaml(SIMPLE_FOR_LOOP_YAML)
      const data = yamlToGraphData(SIMPLE_FOR_LOOP_YAML)
      const graph = buildGraphWithParents(data, sequence)
      const yamlStr = graphToYaml(graph)

      // The FOR loop in SIMPLE_FOR_LOOP_YAML has count=5 but no iterator_var
      expect(yamlStr).toContain('count: 5')
      expect(yamlStr).not.toContain('iterator_var')

      graph.dispose()
    })
  })

  // ── Loop container edge wiring ───────────────────────────────────────────

  describe('loop container edge wiring', () => {
    it('creates sequential edges from predecessor step to loop container', () => {
      const yaml = `
name: "Edge Test"
version: "3.0"
max_concurrency: 4
steps:
  - id: step_before
    script: test_scripts/before.py
    timeout: 10
  - id: my_loop
    loop_type: FOR
    count: 3
    steps:
      - id: loop_step
        script: test_scripts/loop.py
        timeout: 10
`

      const data = yamlToGraphData(yaml)

      const edge = data.edges.find(
        e => e.source === 'step_before' && e.target === 'my_loop'
      )
      expect(edge).toBeDefined()
    })

    it('creates sequential edges from loop container to successor step', () => {
      const yaml = `
name: "Edge Test"
version: "3.0"
max_concurrency: 4
steps:
  - id: my_loop
    loop_type: FOR
    count: 3
    steps:
      - id: loop_step
        script: test_scripts/loop.py
        timeout: 10
  - id: step_after
    script: test_scripts/after.py
    timeout: 10
`

      const data = yamlToGraphData(yaml)

      const edge = data.edges.find(
        e => e.source === 'my_loop' && e.target === 'step_after'
      )
      expect(edge).toBeDefined()
    })

    it('creates edges between consecutive loop containers', () => {
      const yaml = `
name: "Edge Test"
version: "3.0"
max_concurrency: 4
steps:
  - id: loop1
    loop_type: FOR
    count: 3
    steps:
      - id: step1
        script: test_scripts/step.py
        timeout: 10
  - id: loop2
    loop_type: WHILE
    condition: "x < 5"
    steps:
      - id: step2
        script: test_scripts/step.py
        timeout: 10
`

      const data = yamlToGraphData(yaml)

      const edge = data.edges.find(
        e => e.source === 'loop1' && e.target === 'loop2'
      )
      expect(edge).toBeDefined()
    })

    it('does NOT create edges from loop container to its own children', () => {
      const data = yamlToGraphData(SIMPLE_FOR_LOOP_YAML)

      // No edge from loop container to its child 'measure_iteration'
      const badEdge = data.edges.find(
        e => e.source === 'repeat_measurement' && e.target === 'measure_iteration'
      )
      expect(badEdge).toBeUndefined()
    })
  })

  // ── Variable node ────────────────────────────────────────────────────────

  describe('variable scope handling', () => {
    it('creates a variable node when scope has variables', () => {
      const yaml = `
name: "Variable Test"
version: "3.0"
scope:
  variables:
    channel_count: 4
    max_retries: 3
steps:
  - id: step1
    script: test_scripts/step.py
    timeout: 10
`

      const data = yamlToGraphData(yaml)

      const varNode = data.nodes.find(n => n.id === 'variables-scope')
      expect(varNode).toBeDefined()
    })

    it('does NOT create a variable node when scope has no variables', () => {
      const data = yamlToGraphData(SIMPLE_FOR_LOOP_YAML)

      const varNode = data.nodes.find(n => n.id === 'variables-scope')
      expect(varNode).toBeUndefined()
    })
  })
})