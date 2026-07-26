import dagre from 'dagre'
import type { GraphData, NodeConfig } from './useSerializer'
import type { NodeData } from '@/models/nodes/types'
import { isLoopContainerData, isVariableData, isScriptStepData } from '@/models/nodes/types'

/**
 * Node dimension estimates by type.
 * These are used to give dagre an idea of node sizes for layout calculation.
 * Actual rendering may differ, but these approximations produce good layouts.
 */
const NODE_DIMENSIONS: Record<string, { width: number; height: number }> = {
  scriptStep: { width: 240, height: 80 },
  variable: { width: 200, height: 60 },
  loopContainer: { width: 300, height: 120 },
}

/**
 * Default dagre layout configuration.
 * - rankdir=LR: left-to-right directed layout (DAG flows left→right)
 * - nodesep=200: horizontal spacing between nodes in the same rank
 * - ranksep=150: vertical spacing between ranks
 * - marginx=20: left/right margin within each node's bounding box
 * - marginy=20: top/bottom margin within each node's bounding box
 */
const DEFAULT_LAYOUT_CONFIG: dagre.GraphLabel = {
  rankdir: 'LR',
  nodesep: 200,
  ranksep: 150,
  marginx: 20,
  marginy: 20,
}

/**
 * Auto-layout options.
 */
export interface AutoLayoutOptions {
  /** Whether auto-layout is enabled. Default: true. */
  enabled?: boolean
  /** Custom dagre graph label config (merged with defaults). */
  graphConfig?: dagre.GraphLabel
}

/**
 * Determine the estimated dimensions for a node based on its data type.
 */
function getNodeDimensions(data: NodeData): { width: number; height: number } {
  if (isLoopContainerData(data)) return NODE_DIMENSIONS.loopContainer
  if (isVariableData(data)) return NODE_DIMENSIONS.variable
  if (isScriptStepData(data)) return NODE_DIMENSIONS.scriptStep
  return { width: 240, height: 80 } // fallback
}

/**
 * Apply dagre auto-layout to a GraphData object.
 *
 * Uses dagre's hierarchical layout algorithm to position nodes based on
 * edge dependencies (the DAG structure). Nodes with no incoming edges
 * are placed on the leftmost rank; edges determine the flow direction
 * (left to right, per rankdir=LR).
 *
 * Loop container children are NOT re-laid out — they use relative
 * coordinates within their parent container and are skipped.
 *
 * @param graphData - The graph data with nodes and edges to lay out.
 * @param options - Optional layout configuration.
 * @returns A new GraphData with node positions computed by dagre.
 */
export function autoLayout(graphData: GraphData, options: AutoLayoutOptions = {}): GraphData {
  const { enabled = true, graphConfig = {} } = options

  if (!enabled) {
    return graphData
  }

  const { nodes, edges } = graphData

  if (nodes.length === 0) {
    return { ...graphData, nodes: [], edges: [...edges] }
  }

  // Create dagre graph
  const g = new dagre.graphlib.Graph()
  g.setGraph({ ...DEFAULT_LAYOUT_CONFIG, ...graphConfig })
  g.setDefaultEdgeLabel(() => ({}))

  // Add nodes to dagre graph (only top-level nodes; skip child nodes
  // which already have relative offsets set within their parent containers).
  // Child nodes are detected by having non-zero positions (top-level nodes
  // are seeded at 0,0 before dagre runs).
  const childNodeIds = new Set<string>()
  for (const node of nodes) {
    if (node.x !== 0 || node.y !== 0) {
      // This node already has a position — it's a child inside a container
      childNodeIds.add(node.id)
      continue
    }
    const dims = getNodeDimensions(node.data)
    g.setNode(node.id, { width: dims.width, height: dims.height })
  }

  // Add edges to dagre graph (only top-level edges — skip edges where
  // either endpoint is a child node)
  for (const edge of edges) {
    if (childNodeIds.has(edge.source) || childNodeIds.has(edge.target)) {
      continue
    }
    if (!g.hasNode(edge.source) || !g.hasNode(edge.target)) {
      continue
    }
    g.setEdge(edge.source, edge.target)
  }

  // Run dagre layout
  dagre.layout(g)

  // Extract computed positions (only for nodes that were added to dagre)
  const positionedNodes: NodeConfig[] = nodes.map((node) => {
    if (childNodeIds.has(node.id)) {
      // Child node — keep its original relative position
      return node
    }
    if (g.hasNode(node.id)) {
      const dagreNode = g.node(node.id)
      return {
        ...node,
        x: dagreNode.x - dagreNode.width / 2,
        y: dagreNode.y - dagreNode.height / 2,
      }
    }
    // Node wasn't added to dagre (should not happen) — keep original position
    return node
  })

  return {
    ...graphData,
    nodes: positionedNodes,
  }
}

/**
 * Composable for auto-layout functionality.
 *
 * @returns The autoLayout function for use in Vue composables.
 */
export function useAutoLayout() {
  return {
    autoLayout,
  }
}