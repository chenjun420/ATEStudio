import type { Graph } from '@antv/x6'
import type { Cell } from '@antv/x6'
import { ref, onUnmounted } from 'vue'
import type { Ref } from 'vue'
import type { DependencyCheckInput, DependencyCheckOutput } from '@/workers/dependencyCheck.worker'

/**
 * Composable for detecting circular dependencies in X6 graph
 * Uses DFS to check if adding an edge would create a cycle
 * Supports scoped cycle detection: back-edges within loop containers are allowed
 *
 * For graphs with > NODE_THRESHOLD nodes, cycle detection is offloaded to a
 * Web Worker to avoid blocking the main thread.
 */

const NODE_THRESHOLD = 50

/**
 * Find the parent container node for a given node
 * Uses X6's parent/child API to traverse up the containment hierarchy
 * @param graph - The X6 graph instance
 * @param nodeId - The node ID to find the container for
 * @returns The parent container Cell if one exists, undefined otherwise
 */
export function findContainerForNode(graph: Graph, nodeId: string): Cell | undefined {
  const cell = graph.getCellById(nodeId)
  if (!cell) return undefined
  return cell.getParent() ?? undefined
}

/**
 * Check if both source and target nodes are within the same container
 * @param graph - The X6 graph instance
 * @param sourceId - The source node ID
 * @param targetId - The target node ID
 * @param containerId - The container node ID to check against
 * @returns true if both nodes share the specified parent container
 */
export function isWithinContainer(
  graph: Graph,
  sourceId: string,
  targetId: string,
  containerId: string
): boolean {
  const sourceParent = findContainerForNode(graph, sourceId)
  const targetParent = findContainerForNode(graph, targetId)
  // Both must have the same parent, and that parent must match containerId
  return sourceParent?.id === containerId && targetParent?.id === containerId
}

/**
 * Serialize graph nodes and edges into arrays for Worker transfer.
 * Extracts all node IDs and edge [source, target] pairs.
 */
function serializeGraph(graph: Graph): { nodes: string[]; edges: [string, string][] } {
  const nodes = graph.getNodes().map(n => n.id)
  const edges: [string, string][] = []
  graph.getEdges().forEach(edge => {
    const source = edge.getSourceCellId()
    const target = edge.getTargetCellId()
    if (source && target) {
      edges.push([source, target])
    }
  })
  return { nodes, edges }
}

/**
 * Check if adding an edge from source to target would create a cycle,
 * offloading to a Web Worker for large graphs.
 *
 * @param worker - The Web Worker instance to use
 * @param graph - The X6 graph instance
 * @param sourceId - The source node ID
 * @param targetId - The target node ID
 * @returns Promise resolving to true if adding the edge would create a cycle
 */
async function wouldCreateCycleWorker(
  worker: Worker,
  graph: Graph,
  sourceId: string,
  targetId: string
): Promise<boolean> {
  const { nodes, edges } = serializeGraph(graph)

  return new Promise<boolean>((resolve) => {
    const handler = (event: MessageEvent<DependencyCheckOutput>) => {
      worker.removeEventListener('message', handler)
      resolve(event.data.hasCycle)
    }
    worker.addEventListener('message', handler)

    const input: DependencyCheckInput & { sourceId: string; targetId: string } = {
      nodes,
      edges,
      sourceId,
      targetId,
    }
    worker.postMessage(input)
  })
}

/**
 * Check if adding an edge from source to target would create a cycle
 * @param graph - The X6 graph instance
 * @param sourceId - The source node ID
 * @param targetId - The target node ID
 * @param containerId - Optional container ID: if both nodes are within this container, back-edges are allowed
 * @returns true if adding the edge would create a cycle, false otherwise
 */
export function wouldCreateCycle(
  graph: Graph,
  sourceId: string,
  targetId: string,
  containerId?: string
): boolean {
  // If containerId is provided and both nodes are within the same container,
  // back-edges are allowed (loops need them) — not a cycle in this scope
  if (containerId && isWithinContainer(graph, sourceId, targetId, containerId)) {
    return false
  }

  // Build adjacency list (node -> list of nodes it points to)
  const adjacencyList = new Map<string, Set<string>>()

  // Initialize all nodes
  graph.getNodes().forEach(node => {
    adjacencyList.set(node.id, new Set())
  })

  // Build adjacency list from edges
  const edges = graph.getEdges()
  edges.forEach(edge => {
    const source = edge.getSourceCellId()
    const target = edge.getTargetCellId()
    if (source && target) {
      const targets = adjacencyList.get(source)
      if (targets) {
        targets.add(target)
      }
    }
  })

  // DFS to check if target can reach source
  // If target can reach source, then adding source->target creates a cycle
  return canReach(adjacencyList, targetId, sourceId)
}

/**
 * DFS helper to check if start node can reach end node
 */
function canReach(adjacencyList: Map<string, Set<string>>, startId: string, endId: string): boolean {
  const visited = new Set<string>()
  const stack: string[] = [startId]

  while (stack.length > 0) {
    const current = stack.pop()!

    if (current === endId) {
      return true
    }

    if (visited.has(current)) {
      continue
    }

    visited.add(current)

    const neighbors = adjacencyList.get(current)
    if (neighbors) {
      neighbors.forEach(neighbor => {
        if (!visited.has(neighbor)) {
          stack.push(neighbor)
        }
      })
    }
  }

  return false
}

/**
 * Get all nodes in a dependency chain starting from a node
 * @param graph - The X6 graph instance
 * @param nodeId - Starting node ID
 * @param direction - 'upstream' or 'downstream'
 * @returns Set of node IDs in the dependency chain
 */
export function getDependencyChain(
  graph: Graph,
  nodeId: string,
  direction: 'upstream' | 'downstream'
): Set<string> {
  const result = new Set<string>()
  const edges = graph.getEdges()

  // Build adjacency list based on direction
  const adjacencyList = new Map<string, Set<string>>()

  graph.getNodes().forEach(node => {
    adjacencyList.set(node.id, new Set())
  })

  edges.forEach(edge => {
    const source = edge.getSourceCellId()
    const target = edge.getTargetCellId()
    if (source && target) {
      if (direction === 'downstream') {
        // Follow edges in forward direction (source -> target)
        adjacencyList.get(source)?.add(target)
      } else {
        // Follow edges in reverse direction (target -> source)
        adjacencyList.get(target)?.add(source)
      }
    }
  })

  // DFS to find all reachable nodes
  const stack: string[] = [nodeId]
  const visited = new Set<string>()

  while (stack.length > 0) {
    const current = stack.pop()!

    if (visited.has(current)) {
      continue
    }

    visited.add(current)
    result.add(current)

    const neighbors = adjacencyList.get(current)
    if (neighbors) {
      neighbors.forEach(neighbor => {
        if (!visited.has(neighbor)) {
          stack.push(neighbor)
        }
      })
    }
  }

  // Remove the starting node from result (we want dependencies, not the node itself)
  result.delete(nodeId)

  return result
}

/**
 * Validate a potential connection
 * @param graph - The X6 graph instance
 * @param sourceId - Source node ID
 * @param targetId - Target node ID
 * @param containerId - Optional container ID for scoped cycle detection
 * @returns Object with valid flag and error message if invalid
 */
export function validateConnection(
  graph: Graph,
  sourceId: string,
  targetId: string,
  containerId?: string
): { valid: boolean; error?: string } {
  // Self-loop is not allowed
  if (sourceId === targetId) {
    return { valid: false, error: 'Cannot connect a node to itself' }
  }

  // Check for cycle (scoped if containerId provided)
  if (wouldCreateCycle(graph, sourceId, targetId, containerId)) {
    return { valid: false, error: 'This connection would create a circular dependency' }
  }

  return { valid: true }
}

/**
 * Composable function for dependency checking.
 *
 * Returns `wouldCreateCycleAsync`: for graphs with >50 nodes, offloads to a
 * Web Worker (lazily created and reused). For ≤50 nodes, runs synchronously
 * on the UI thread. Also exposes `isChecking` for loading state.
 */
export function useDependencyCheck() {
  const isChecking: Ref<boolean> = ref(false)
  let worker: Worker | null = null

  function getWorker(): Worker {
    if (!worker) {
      worker = new Worker(
        new URL('@/workers/dependencyCheck.worker.ts', import.meta.url),
        { type: 'module' }
      )
    }
    return worker
  }

  /**
   * Check if adding an edge would create a cycle.
   * For graphs with >50 nodes, delegates to a Web Worker (returns Promise).
   * For ≤50 nodes, runs synchronously on UI thread.
   */
  async function wouldCreateCycleAsync(
    graph: Graph,
    sourceId: string,
    targetId: string,
    containerId?: string
  ): Promise<boolean> {
    // Container-scoped back-edges are always allowed (no cycle in outer scope)
    if (containerId && isWithinContainer(graph, sourceId, targetId, containerId)) {
      return false
    }

    const nodeCount = graph.getNodes().length

    if (nodeCount <= NODE_THRESHOLD) {
      // Small graph: run on UI thread synchronously
      return wouldCreateCycle(graph, sourceId, targetId, containerId)
    }

    // Large graph: offload to Web Worker
    isChecking.value = true
    try {
      const w = getWorker()
      return await wouldCreateCycleWorker(w, graph, sourceId, targetId)
    } finally {
      isChecking.value = false
    }
  }

  // Terminate worker on component unmount
  onUnmounted(() => {
    if (worker) {
      worker.terminate()
      worker = null
    }
  })

  return {
    wouldCreateCycle,
    wouldCreateCycleAsync,
    validateConnection,
    getDependencyChain,
    findContainerForNode,
    isWithinContainer,
    isChecking,
  }
}
