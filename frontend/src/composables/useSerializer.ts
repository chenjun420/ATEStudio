import * as yaml from 'js-yaml'
import type { Graph, Node, Edge } from '@antv/x6'
import type { ScriptStepData, VariableData, LoopContainerData, NodeData } from '@/models/nodes/types'
import type { EdgeData } from '@/models/edges/types'
import { isScriptStepData, isVariableData, isLoopContainerData } from '@/models/nodes/types'
import type { LoopTypeValue, ExecutionModeValue } from '@/types/dsl'

/**
 * YAML DSL v3.0 Sequence Definition
 * Types imported from the canonical generated schema (frontend/src/types/dsl.ts)
 */
import type { YamlSequence, YamlStep, YamlLoop, YamlScope } from '@/types/dsl'

export type { YamlSequence, YamlStep, YamlLoop, YamlScope }

/**
 * Type guard: check if a step entry is a YamlStep (not a YamlLoop)
 */
function isYamlStep(step: YamlStep | YamlLoop): step is YamlStep {
  return 'script' in step
}

/**
 * Type guard: check if a step entry is a YamlLoop (not a YamlStep)
 */
function isYamlLoop(step: YamlStep | YamlLoop): step is YamlLoop {
  return 'loop_type' in step
}

/**
 * Map LoopContainerData.loopType (lowercase) to YamlLoop loop_type (UPPERCASE)
 */
function toDslLoopType(loopType: 'for' | 'while' | 'foreach'): LoopTypeValue {
  const mapping: Record<string, LoopTypeValue> = {
    for: 'FOR',
    while: 'WHILE',
    foreach: 'FOREACH',
  }
  return mapping[loopType] || 'FOR'
}

/**
 * Map YamlLoop loop_type (UPPERCASE) to LoopContainerData.loopType (lowercase)
 */
function fromDslLoopType(loopType: LoopTypeValue): 'for' | 'while' | 'foreach' {
  const mapping: Record<string, 'for' | 'while' | 'foreach'> = {
    FOR: 'for',
    WHILE: 'while',
    FOREACH: 'foreach',
  }
  return mapping[loopType] || 'for'
}

/**
 * Map LoopContainerData.executionMode (lowercase) to YamlLoop execution_mode (UPPERCASE)
 */
function toDslExecutionMode(mode: 'serial' | 'parallel'): ExecutionModeValue {
  return mode === 'parallel' ? 'PARALLEL' : 'SERIAL'
}

/**
 * Map YamlLoop execution_mode (UPPERCASE) to LoopContainerData.executionMode (lowercase)
 */
function fromDslExecutionMode(mode: ExecutionModeValue | undefined): 'serial' | 'parallel' {
  return mode === 'PARALLEL' ? 'parallel' : 'serial'
}

/**
 * Parsed graph data for import
 */
export interface GraphData {
  nodes: NodeConfig[]
  edges: EdgeConfig[]
  sequence: YamlSequence
}

export interface NodeConfig {
  id: string
  x: number
  y: number
  data: NodeData
}

export interface EdgeConfig {
  id?: string
  source: string
  target: string
  sourcePort?: string
  targetPort?: string
  data?: EdgeData
}

/**
 * Serializer options
 */
export interface SerializerOptions {
  name?: string
  version?: string
  defaultTimeout?: number
}

const DEFAULT_OPTIONS: SerializerOptions = {
  name: 'Untitled Sequence',
  version: '3.0',
  defaultTimeout: 60,
}

/**
 * Convert X6 Graph to YAML string (DSL v3.0 format)
 */
export function graphToYaml(graph: Graph, options: SerializerOptions = {}): string {
  const opts = { ...DEFAULT_OPTIONS, ...options }

  // Collect all nodes and edges
  const nodes = graph.getNodes()
  const edges = graph.getEdges()

  // Build precondition map from edges (top-level edges only)
  const preconditionMap = buildPreconditionMap(edges)

  // Separate top-level nodes from child nodes
  const topLevelNodes: Node[] = []
  const childNodeSet = new Set<string>()

  for (const node of nodes) {
    if (node.getParent()) {
      childNodeSet.add(node.id)
    } else {
      topLevelNodes.push(node)
    }
  }

  // Convert top-level nodes to YAML steps/loops
  const steps: Array<YamlStep | YamlLoop> = []
  let variables: Record<string, unknown> = {}

  for (const node of topLevelNodes) {
    const data = node.getData() as NodeData

    if (isScriptStepData(data)) {
      const step = convertScriptStepToYaml(node, data, preconditionMap)
      steps.push(step)
    } else if (isLoopContainerData(data)) {
      const loop = convertLoopContainerToYaml(node, data, graph)
      steps.push(loop)
    } else if (isVariableData(data)) {
      variables = { ...variables, ...data.variables }
    }
  }

  // Sort steps by topological order (based on preconditions)
  const sortedSteps = topologicalSort(steps, preconditionMap)

  // Build YAML sequence object
  const sequence: YamlSequence = {
    name: opts.name || 'Untitled Sequence',
    version: opts.version || '3.0',
    scope: Object.keys(variables).length > 0 ? { variables } : undefined,
    max_concurrency: 4,
    steps: sortedSteps,
  }

  // Convert to YAML string
  return yaml.dump(sequence, {
    indent: 2,
    lineWidth: -1,
    noRefs: true,
    sortKeys: false,
    forceQuotes: false,
  })
}

/**
 * Convert YAML string to graph data
 */
export function yamlToGraphData(yamlStr: string): GraphData {
  const sequence = yaml.load(yamlStr) as YamlSequence

  if (!sequence || typeof sequence !== 'object') {
    throw new Error('Invalid YAML: must be an object')
  }

  if (!sequence.steps || !Array.isArray(sequence.steps)) {
    throw new Error('Invalid YAML: missing or invalid steps array')
  }

  const nodes: NodeConfig[] = []
  const edges: EdgeConfig[] = []

  // Calculate node positions using topological layout
  const startX = 100
  const startY = 100
  const stepWidth = 280
  const stepHeight = 150
  const loopContainerHeight = 200

  // Build step map for quick lookup (only YamlStep entries, not YamlLoop)
  const stepMap = new Map<string, YamlStep>()
  const loopMap = new Map<string, YamlLoop>()
  sequence.steps.forEach(step => {
    if (isYamlStep(step)) stepMap.set(step.id, step)
    else if (isYamlLoop(step)) loopMap.set(step.id, step)
  })

  // Build dependency graph for layout (only top-level YamlStep entries)
  const inDegree = new Map<string, number>()
  const dependents = new Map<string, string[]>()
  
  sequence.steps.forEach(step => {
    if (!isYamlStep(step)) return
    inDegree.set(step.id, step.preconditions?.length || 0)
    dependents.set(step.id, [])
  })
  
  sequence.steps.forEach(step => {
    if (!isYamlStep(step)) return
    (step.preconditions || []).forEach(precondId => {
      const deps = dependents.get(precondId) || []
      deps.push(step.id)
      dependents.set(precondId, deps)
    })
  })

  // Kahn's algorithm to determine levels
  const levels = new Map<string, number>()
  const queue: string[] = []
  
  sequence.steps.forEach(step => {
    if (!isYamlStep(step)) return
    if (inDegree.get(step.id) === 0) {
      queue.push(step.id)
      levels.set(step.id, 0)
    }
  })
  
  while (queue.length > 0) {
    const current = queue.shift()!
    const currentLevel = levels.get(current) || 0
    
    const deps = dependents.get(current) || []
    deps.forEach(depId => {
      const newDegree = (inDegree.get(depId) || 1) - 1
      inDegree.set(depId, newDegree)
      
      if (newDegree === 0) {
        levels.set(depId, currentLevel + 1)
        queue.push(depId)
      }
    })
  }

  // Handle nodes not in topological order (cycles or isolated) — warn instead of silently appending
  sequence.steps.forEach(step => {
    if (!isYamlStep(step)) return
    if (!levels.has(step.id)) {
      console.warn(`[yamlToGraphData] Step "${step.id}" is in a cycle or unreachable — placing at level 0`)
      levels.set(step.id, 0)
    }
  })

  // Assign levels to loop containers (place them alongside their topological position)
  // Loops don't have preconditions in the DSL, so they get level 0 by default.
  // We position them based on their index among top-level entries.
  let topLevelIndex = 0
  const topLevelLevels = new Map<string, number>()
  for (const step of sequence.steps) {
    if (isYamlStep(step) && levels.has(step.id)) {
      topLevelLevels.set(step.id, levels.get(step.id)!)
    } else if (isYamlLoop(step)) {
      // Loops are placed at the next available level after the last step
      const maxLevel = topLevelLevels.size > 0
        ? Math.max(...topLevelLevels.values())
        : -1
      topLevelLevels.set(step.id, maxLevel + 1)
    }
    topLevelIndex++
  }

  // Group top-level entries by level
  const levelNodes = new Map<number, Array<{ id: string; type: 'step' | 'loop' }>>()
  topLevelLevels.forEach((level, id) => {
    const nodeList = levelNodes.get(level) || []
    const type = loopMap.has(id) ? 'loop' as const : 'step' as const
    nodeList.push({ id, type })
    levelNodes.set(level, nodeList)
  })

  // Create nodes with positions based on topological levels
  levelNodes.forEach((entries, level) => {
    entries.forEach((entry, indexInLevel) => {
      if (entry.type === 'step') {
        const step = stepMap.get(entry.id)
        if (!step) return

        const x = startX + level * stepWidth
        const y = startY + indexInLevel * stepHeight

        const nodeData: ScriptStepData = {
          stepId: step.id,
          scriptName: step.script,
          scriptVersion: '',
          params: step.params || {},
          preconditions: step.preconditions || [],
          resources: step.resources ? Object.keys(step.resources) : [],
          timeout: step.timeout || 60000,
          onFail: (step.on_fail as 'stop' | 'skip' | 'ignore') || 'stop',
          exportOutputs: step.export_outputs || false,
          status: 'idle',
        }

        nodes.push({
          id: step.id,
          x,
          y,
          data: nodeData,
        })
      } else {
        // Loop container
        const loop = loopMap.get(entry.id)
        if (!loop) return

        const x = startX + level * stepWidth
        const y = startY + indexInLevel * (loopContainerHeight + 50)

        // Create loop container node
        const containerData: LoopContainerData = {
          loopId: loop.id,
          loopType: fromDslLoopType(loop.loop_type),
          condition: loop.condition || '',
          iterationVar: loop.iterator_var || undefined,
          collectionExpr: loop.collection || undefined,
          count: loop.count || undefined,
          executionMode: fromDslExecutionMode(loop.execution_mode),
          maxConcurrency: loop.max_iterations || 1,
          status: 'idle',
        }

        nodes.push({
          id: loop.id,
          x,
          y,
          data: containerData,
        })

        // Recursively create child nodes from loop.steps
        if (loop.steps && loop.steps.length > 0) {
          const childResult = createChildNodesFromLoopSteps(
            loop.steps,
            x + 20,  // offset inside container
            y + 40,   // offset below label
            loop.id
          )
          nodes.push(...childResult.nodes)
          edges.push(...childResult.edges)
        }
      }
    })
  })

  // Create edges from preconditions (only top-level YamlStep entries)
  sequence.steps.forEach((step) => {
    if (!isYamlStep(step)) return
    if (step.preconditions && step.preconditions.length > 0) {
      step.preconditions.forEach((precondId) => {
        edges.push({
          source: precondId,
          target: step.id,
          data: {
            condition: { status: 'passed' },
          },
        })
      })
    }
  })

  // Add variable node if scope has variables
  if (sequence.scope?.variables && Object.keys(sequence.scope.variables).length > 0) {
    const varNodeId = 'variables-scope'
    nodes.push({
      id: varNodeId,
      x: startX - stepWidth,
      y: startY,
      data: {
        variables: sequence.scope.variables,
      } as VariableData,
    })
  }

  return {
    nodes,
    edges,
    sequence,
  }
}

/**
 * Recursively create child nodes and edges from a YamlLoop's steps array.
 * Returns NodeConfig[] and EdgeConfig[] with parent set to the container node ID.
 */
function createChildNodesFromLoopSteps(
  steps: Array<YamlStep | YamlLoop>,
  offsetX: number,
  offsetY: number,
  parentContainerId: string
): { nodes: NodeConfig[]; edges: EdgeConfig[] } {
  const nodes: NodeConfig[] = []
  const edges: EdgeConfig[] = []
  const childStepWidth = 240
  const childLoopWidth = 280

  // Build internal precondition map and topological sort for child steps
  const childPreconditionMap = new Map<string, string[]>()
  const childStepEntries: YamlStep[] = []
  const childLoopEntries: YamlLoop[] = []

  for (const step of steps) {
    if (isYamlStep(step)) {
      childStepEntries.push(step)
      // Build precondition map from step's own preconditions
      // (these reference other steps within the same loop)
      if (step.preconditions && step.preconditions.length > 0) {
        childPreconditionMap.set(step.id, [...step.preconditions])
      }
    } else if (isYamlLoop(step)) {
      childLoopEntries.push(step)
    }
  }

  // Topological sort child steps (only YamlStep entries, not YamlLoop)
  const sortedChildSteps = topologicalSortSteps(childStepEntries, childPreconditionMap)

  // Position child steps
  let currentX = offsetX
  const currentY = offsetY

  // Place sorted steps first
  for (const step of sortedChildSteps) {
    const nodeData: ScriptStepData = {
      stepId: step.id,
      scriptName: step.script,
      scriptVersion: '',
      params: step.params || {},
      preconditions: step.preconditions || [],
      resources: step.resources ? Object.keys(step.resources) : [],
      timeout: step.timeout || 60000,
      onFail: (step.on_fail as 'stop' | 'skip' | 'ignore') || 'stop',
      exportOutputs: step.export_outputs || false,
      status: 'idle',
    }

    nodes.push({
      id: step.id,
      x: currentX,
      y: currentY,
      data: nodeData,
    })

    currentX += childStepWidth
  }

  // Place child loops after steps
  for (const loop of childLoopEntries) {
    const containerData: LoopContainerData = {
      loopId: loop.id,
      loopType: fromDslLoopType(loop.loop_type),
      condition: loop.condition || '',
      iterationVar: loop.iterator_var || undefined,
      collectionExpr: loop.collection || undefined,
      count: loop.count || undefined,
      executionMode: fromDslExecutionMode(loop.execution_mode),
      maxConcurrency: loop.max_iterations || 1,
      status: 'idle',
    }

    nodes.push({
      id: loop.id,
      x: currentX,
      y: currentY,
      data: containerData,
    })

    // Recursively create nested child nodes
    if (loop.steps && loop.steps.length > 0) {
      const nestedResult = createChildNodesFromLoopSteps(
        loop.steps,
        currentX + 20,
        currentY + 40,
        loop.id
      )
      nodes.push(...nestedResult.nodes)
      edges.push(...nestedResult.edges)
    }

    currentX += childLoopWidth
  }

  // Create edges from child step preconditions
  for (const step of sortedChildSteps) {
    if (step.preconditions && step.preconditions.length > 0) {
      for (const precondId of step.preconditions) {
        edges.push({
          source: precondId,
          target: step.id,
          data: {
            condition: { status: 'passed' },
          },
        })
      }
    }
  }

  // Create loop-back edge: from last child to first child within the container
  // This represents the loop iteration cycle
  const allChildStepIds = sortedChildSteps.map(s => s.id)
  if (allChildStepIds.length >= 2) {
    const firstChildId = allChildStepIds[0]
    const lastChildId = allChildStepIds[allChildStepIds.length - 1]
    edges.push({
      id: `loop-back-${parentContainerId}`,
      source: lastChildId,
      target: firstChildId,
      sourcePort: `output-${lastChildId}`,
      targetPort: `input-${firstChildId}`,
      data: {
        condition: { status: 'passed' },
      },
    })
  }

  return { nodes, edges }
}

/**
 * Build a map of node IDs to their precondition source IDs.
 * Skips loop-back edges (edges connecting to 'loop-back' port group) since
 * those represent iteration cycles, not control-flow dependencies.
 */
function buildPreconditionMap(edges: Edge[]): Map<string, string[]> {
  const map = new Map<string, string[]>()

  for (const edge of edges) {
    const source = edge.getSourceNode()
    const target = edge.getTargetNode()

    if (!source || !target) continue

    const targetId = target.id
    const sourceId = source.id

    // Skip loop-back edges — they connect to the 'loop-back' port group
    // and represent iteration cycles, not control-flow preconditions
    const sourcePortId = (edge.getSource() as { port?: string })?.port
    const targetPortId = (edge.getTarget() as { port?: string })?.port
    if (sourcePortId?.includes('loop-back') || targetPortId?.includes('loop-back')) {
      continue
    }

    // Get edge data to check condition type
    const edgeData = edge.getData() as EdgeData | undefined

    // Only add as precondition if it's a control flow edge (passed/any condition)
    const conditionStatus = edgeData?.condition?.status
    if (!conditionStatus || conditionStatus === 'passed' || conditionStatus === 'any') {
      const existing = map.get(targetId) || []
      if (!existing.includes(sourceId)) {
        existing.push(sourceId)
        map.set(targetId, existing)
      }
    }
  }

  return map
}

/**
 * Convert a script step node to YAML step format
 */
function convertScriptStepToYaml(
  node: Node,
  data: ScriptStepData,
  preconditionMap: Map<string, string[]>
): YamlStep {
  return {
    id: data.stepId || node.id,
    script: data.scriptName,
    params: data.params || {},
    preconditions: preconditionMap.get(node.id) || [],
    // Frontend resources is string[]; backend uses Record<string, unknown>
    resources: (data.resources || []).reduce<Record<string, unknown>>((acc, r) => { acc[r] = true; return acc }, {}),
    timeout: Math.ceil((data.timeout || 60000) / 1000), // Convert ms to seconds
    on_fail: data.onFail || 'stop',
    export_outputs: data.exportOutputs || false,
  }
}

/**
 * Convert a loop-container node to YamlLoop format, recursively serializing children.
 */
function convertLoopContainerToYaml(
  node: Node,
  data: LoopContainerData,
  graph: Graph
): YamlLoop {
  // Get child nodes of this container
  const children = node.getChildren()
  const childNodes: Node[] = []
  if (children) {
    for (const child of children) {
      if (child.isNode()) {
        childNodes.push(child as Node)
      }
    }
  }

  // Build internal precondition map from edges within the container
  const allEdges = graph.getEdges()
  const childNodeIds = new Set(childNodes.map(n => n.id))
  const internalEdges = allEdges.filter(edge => {
    const sourceCell = edge.getSourceCell()
    const targetCell = edge.getTargetCell()
    return sourceCell && targetCell &&
      childNodeIds.has(sourceCell.id) && childNodeIds.has(targetCell.id)
  })
  const internalPreconditionMap = buildPreconditionMap(internalEdges)

  // Recursively convert child nodes to YamlStep/YamlLoop
  const childSteps: Array<YamlStep | YamlLoop> = []
  for (const childNode of childNodes) {
    const childData = childNode.getData() as NodeData
    if (isScriptStepData(childData)) {
      childSteps.push(convertScriptStepToYaml(childNode, childData, internalPreconditionMap))
    } else if (isLoopContainerData(childData)) {
      childSteps.push(convertLoopContainerToYaml(childNode, childData, graph))
    }
  }

  // Sort child steps topologically within the loop
  const sortedChildSteps = topologicalSort(childSteps, internalPreconditionMap)

  // Build YamlLoop object
  const loop: YamlLoop = {
    id: data.loopId || node.id,
    loop_type: toDslLoopType(data.loopType),
    steps: sortedChildSteps,
  }

  // Add optional fields only when they have meaningful values
  if (data.count != null) {
    loop.count = data.count
  }
  if (data.condition) {
    loop.condition = data.condition
  }
  if (data.collectionExpr) {
    loop.collection = data.collectionExpr
  }
  if (data.iterationVar) {
    loop.iterator_var = data.iterationVar
  }
  if (data.executionMode === 'parallel') {
    loop.execution_mode = toDslExecutionMode(data.executionMode)
  }
  if (data.maxConcurrency && data.maxConcurrency > 1) {
    loop.max_iterations = data.maxConcurrency
  }

  return loop
}

/**
 * Topological sort steps based on preconditions.
 * Handles both YamlStep and YamlLoop entries.
 * YamlLoop entries without preconditions are treated as having in-degree 0.
 * If a cycle is detected, logs a warning and appends remaining items at the end
 * (instead of silently appending without notification).
 */
function topologicalSort(
  steps: Array<YamlStep | YamlLoop>,
  preconditionMap: Map<string, string[]>
): Array<YamlStep | YamlLoop> {
  const stepMap = new Map<string, YamlStep | YamlLoop>()
  const inDegree = new Map<string, number>()
  const result: Array<YamlStep | YamlLoop> = []

  // Initialize
  for (const step of steps) {
    stepMap.set(step.id, step)
    // YamlLoop entries don't have preconditions in the top-level sort
    const preconds = isYamlStep(step) ? (preconditionMap.get(step.id) || []) : []
    inDegree.set(step.id, preconds.length)
  }

  // Kahn's algorithm
  const queue: string[] = []

  // Start with nodes that have no preconditions
  for (const step of steps) {
    if (inDegree.get(step.id) === 0) {
      queue.push(step.id)
    }
  }

  while (queue.length > 0) {
    const currentId = queue.shift()!
    const currentStep = stepMap.get(currentId)
    if (currentStep) {
      result.push(currentStep)
    }

    // Find all steps that depend on current
    for (const step of steps) {
      const preconds = isYamlStep(step) ? (preconditionMap.get(step.id) || []) : []
      if (preconds.includes(currentId)) {
        const newDegree = (inDegree.get(step.id) || 0) - 1
        inDegree.set(step.id, newDegree)
        if (newDegree === 0) {
          queue.push(step.id)
        }
      }
    }
  }

  // If we couldn't sort all steps (cycle detected), warn and append remaining
  if (result.length < steps.length) {
    const orphanIds = steps
      .filter(s => !result.find(r => r.id === s.id))
      .map(s => s.id)
    console.warn(
      `[topologicalSort] Cycle detected — ${orphanIds.length} step(s) could not be sorted: ${orphanIds.join(', ')}. ` +
      `Appending at end (execution order may be incorrect).`
    )
    for (const step of steps) {
      if (!result.find((s) => s.id === step.id)) {
        result.push(step)
      }
    }
  }

  return result
}

/**
 * Topological sort for YamlStep-only arrays (no YamlLoop entries).
 * Used for sorting child steps within a loop container where only YamlStep
 * entries have preconditions.
 * If a cycle is detected, logs a warning and appends remaining items.
 */
function topologicalSortSteps(
  steps: YamlStep[],
  preconditionMap: Map<string, string[]>
): YamlStep[] {
  const stepMap = new Map<string, YamlStep>()
  const inDegree = new Map<string, number>()
  const result: YamlStep[] = []

  // Initialize
  for (const step of steps) {
    stepMap.set(step.id, step)
    const preconds = preconditionMap.get(step.id) || []
    inDegree.set(step.id, preconds.length)
  }

  // Kahn's algorithm
  const queue: string[] = []

  for (const step of steps) {
    if (inDegree.get(step.id) === 0) {
      queue.push(step.id)
    }
  }

  while (queue.length > 0) {
    const currentId = queue.shift()!
    const currentStep = stepMap.get(currentId)
    if (currentStep) {
      result.push(currentStep)
    }

    for (const step of steps) {
      const preconds = preconditionMap.get(step.id) || []
      if (preconds.includes(currentId)) {
        const newDegree = (inDegree.get(step.id) || 0) - 1
        inDegree.set(step.id, newDegree)
        if (newDegree === 0) {
          queue.push(step.id)
        }
      }
    }
  }

  // If we couldn't sort all steps (cycle detected), warn and append remaining
  if (result.length < steps.length) {
    const orphanIds = steps
      .filter(s => !result.find(r => r.id === s.id))
      .map(s => s.id)
    console.warn(
      `[topologicalSortSteps] Cycle detected — ${orphanIds.length} step(s) could not be sorted: ${orphanIds.join(', ')}. ` +
      `Appending at end (execution order may be incorrect).`
    )
    for (const step of steps) {
      if (!result.find((s) => s.id === step.id)) {
        result.push(step)
      }
    }
  }

  return result
}

/**
 * Export graph to file download
 */
export function exportGraphToYaml(graph: Graph, filename: string = 'sequence.yaml'): void {
  const yamlContent = graphToYaml(graph)
  const blob = new Blob([yamlContent], { type: 'text/yaml' })
  const url = URL.createObjectURL(blob)

  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  URL.revokeObjectURL(url)
}

/**
 * Import YAML file and load into graph
 */
export function importYamlToGraph(
  graph: Graph,
  yamlContent: string,
  clearExisting: boolean = true
): GraphData {
  const graphData = yamlToGraphData(yamlContent)

  // Clear existing graph if requested
  if (clearExisting) {
    graph.clearCells()
  }

  // Identify container node IDs (LoopContainerData nodes)
  const containerNodeIds = new Set<string>()
  for (const nodeConfig of graphData.nodes) {
    if (isLoopContainerData(nodeConfig.data)) {
      containerNodeIds.add(nodeConfig.id)
    }
  }

  // Build parent map: child node ID -> container node ID
  // A child is positioned inside a container if its position falls within the container's bounds
  // For now, we use a simpler approach: child nodes are those whose IDs appear in
  // loop-back edges or whose positions are inside a container.
  // The yamlToGraphData function already positions children inside containers,
  // so we detect parentage by checking if a non-container node's position is
  // within a container's bounding box.
  const parentMap = new Map<string, string>() // childId -> parentId

  for (const nodeConfig of graphData.nodes) {
    if (containerNodeIds.has(nodeConfig.id)) continue // skip containers themselves
    if (isVariableData(nodeConfig.data)) continue // skip variable nodes

    // Check if this node falls inside any container
    for (const containerId of containerNodeIds) {
      const containerConfig = graphData.nodes.find(n => n.id === containerId)
      if (!containerConfig) continue

      // Container is 300x200 by default
      const containerWidth = 300
      const containerHeight = 200
      const cx = containerConfig.x
      const cy = containerConfig.y

      if (nodeConfig.x >= cx && nodeConfig.x <= cx + containerWidth &&
          nodeConfig.y >= cy && nodeConfig.y <= cy + containerHeight) {
        parentMap.set(nodeConfig.id, containerId)
        break
      }
    }
  }

  // Add container nodes first (before children so parent exists)
  for (const nodeConfig of graphData.nodes) {
    if (!containerNodeIds.has(nodeConfig.id)) continue
    if (parentMap.has(nodeConfig.id)) continue // nested container — add with parent later

    const data = nodeConfig.data as LoopContainerData
    console.log(`[importYamlToGraph] Adding loop container ${nodeConfig.id} at position (${nodeConfig.x}, ${nodeConfig.y})`)
    graph.addNode({
      id: nodeConfig.id,
      shape: 'loop-container-node',
      x: nodeConfig.x,
      y: nodeConfig.y,
      label: `${data.loopType} loop\n${data.loopId.slice(0, 8)}`,
      data: nodeConfig.data,
      ports: {
        items: [
          { id: `input-${nodeConfig.id}`, group: 'input' },
          { id: `output-${nodeConfig.id}`, group: 'output' },
          { id: `loop-back-${nodeConfig.id}`, group: 'loop-back' },
        ],
      },
    })
  }

  // Add non-container nodes (including nested containers)
  for (const nodeConfig of graphData.nodes) {
    if (containerNodeIds.has(nodeConfig.id) && !parentMap.has(nodeConfig.id)) continue // already added
    if (isVariableData(nodeConfig.data)) {
      // Variable node
      graph.addNode({
        id: nodeConfig.id,
        shape: 'script-step-node',
        x: nodeConfig.x,
        y: nodeConfig.y,
        label: 'Variables',
        data: nodeConfig.data,
        ports: {
          items: [
            { id: `input-${nodeConfig.id}`, group: 'input' },
            { id: `output-${nodeConfig.id}`, group: 'output' },
          ],
        },
      })
      continue
    }

    const data = nodeConfig.data
    const isContainer = isLoopContainerData(data)
    const shape = isContainer ? 'loop-container-node' : 'script-step-node'
    const label = isScriptStepData(data)
      ? `${data.scriptName}\n${nodeConfig.id.slice(0, 8)}`
      : isContainer
        ? `${(data as LoopContainerData).loopType} loop\n${(data as LoopContainerData).loopId.slice(0, 8)}`
        : nodeConfig.id.slice(0, 8)

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

    console.log(`[importYamlToGraph] Adding node ${nodeConfig.id} at position (${nodeConfig.x}, ${nodeConfig.y})`)
    const newNode = graph.addNode({
      id: nodeConfig.id,
      shape,
      x: nodeConfig.x,
      y: nodeConfig.y,
      label,
      data: nodeConfig.data,
      ports,
    })

    // Set parent/child relationship if this node is inside a container
    const parentId = parentMap.get(nodeConfig.id)
    if (parentId) {
      const parentCell = graph.getCellById(parentId)
      if (parentCell && parentCell.isNode()) {
        const parentNode = parentCell as Node
        newNode.setParent(parentNode)
        parentNode.addChild(newNode)
      }
    }
  }

  // Add edges
  for (const edgeConfig of graphData.edges) {
    // Determine source/target port based on edge type
    const sourcePort = edgeConfig.sourcePort || `output-${edgeConfig.source}`
    const targetPort = edgeConfig.targetPort || `input-${edgeConfig.target}`

    graph.addEdge({
      source: { cell: edgeConfig.source, port: sourcePort },
      target: { cell: edgeConfig.target, port: targetPort },
      attrs: {
        line: {
          stroke: edgeConfig.data?.condition?.status === 'failed' ? '#ef4444' : '#6b7280',
          strokeWidth: 2,
        },
      },
      data: edgeConfig.data,
    })
  }

  // Auto-fit content after loading
  graph.zoomToFit({ padding: 40, maxScale: 1 })

  return graphData
}

/**
 * Composable for serializer functionality
 */
export function useSerializer() {
  return {
    graphToYaml,
    yamlToGraphData,
    exportGraphToYaml,
    importYamlToGraph,
  }
}