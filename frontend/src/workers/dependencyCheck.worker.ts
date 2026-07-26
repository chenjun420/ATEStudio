/**
 * Web Worker for DFS cycle detection in dependency graphs.
 *
 * Receives { nodes: string[], edges: [string, string][] } via postMessage
 * and posts back { hasCycle: boolean, cyclePath?: string[] }.
 *
 * The DFS algorithm is the same as the UI-thread version in useDependencyCheck.ts.
 * Offloading to a Worker prevents main-thread blocking for large graphs (>50 nodes).
 */

export interface DependencyCheckInput {
  nodes: string[]
  edges: [string, string][]
}

export interface DependencyCheckOutput {
  hasCycle: boolean
  cyclePath?: string[]
}

/**
 * Build adjacency list from node/edge arrays.
 * Returns a map from node ID → set of nodes it points to.
 */
export function buildAdjacencyList(nodes: string[], edges: [string, string][]): Map<string, Set<string>> {
  const adjacencyList = new Map<string, Set<string>>()
  for (const nodeId of nodes) {
    adjacencyList.set(nodeId, new Set())
  }
  for (const [source, target] of edges) {
    const targets = adjacencyList.get(source)
    if (targets) {
      targets.add(target)
    }
  }
  return adjacencyList
}

/**
 * DFS to check if startId can reach endId in adjacency list.
 * Also records the path from startId to endId for cycle reporting.
 */
export function hasPath(
  adjacencyList: Map<string, Set<string>>,
  startId: string,
  endId: string
): string[] | null {
  const visited = new Set<string>()
  // Use array as stack: each entry is [nodeId, pathSoFar]
  const stack: Array<[string, string[]]> = [[startId, [startId]]]

  while (stack.length > 0) {
    const entry = stack.pop()
    if (!entry) continue
    const [current, path] = entry

    if (current === endId) {
      return path
    }

    if (visited.has(current)) {
      continue
    }

    visited.add(current)

    const neighbors = adjacencyList.get(current)
    if (neighbors) {
      for (const neighbor of neighbors) {
        if (!visited.has(neighbor)) {
          stack.push([neighbor, [...path, neighbor]])
        }
      }
    }
  }

  return null
}

/**
 * Check if adding edge source→target would create a cycle.
 * A cycle exists if target can already reach source.
 */
export function checkCycle(
  nodes: string[],
  edges: [string, string][],
  sourceId: string,
  targetId: string
): { hasCycle: boolean; cyclePath?: string[] } {
  const adjacencyList = buildAdjacencyList(nodes, edges)
  const path = hasPath(adjacencyList, targetId, sourceId)
  if (path) {
    // The cycle would be: path (target→...→source) + source→target
    // path already ends with sourceId, so just append targetId
    return { hasCycle: true, cyclePath: [...path, targetId] }
  }
  return { hasCycle: false }
}

// Worker message handler
self.onmessage = (event: MessageEvent<DependencyCheckInput & { sourceId: string; targetId: string }>) => {
  const { nodes, edges, sourceId, targetId } = event.data

  try {
    const result = checkCycle(nodes, edges, sourceId, targetId)
    self.postMessage(result satisfies DependencyCheckOutput)
  } catch (error) {
    // On error, fallback: no cycle (fail-safe — don't block connections)
    self.postMessage({ hasCycle: false } satisfies DependencyCheckOutput)
  }
}
