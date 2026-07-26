<script setup lang="ts">
/**
 * SubGraphContainer - Renders a sub-graph view for a loop container's child nodes.
 * Creates its own X6 Graph instance with its own coordinate system.
 * Child nodes are extracted from the parent loop container node in the main graph.
 * Changes are synced back to the main graph when exiting the sub-graph view.
 */
import { onMounted, onUnmounted, ref, inject, watch } from 'vue'
import { Graph, History, Keyboard, Clipboard, Snapline } from '@antv/x6'
import type { Ref, ShallowRef } from 'vue'
import type { Node, Edge } from '@antv/x6'
import type { ScriptStepData, NodeData } from '@/models/nodes/types'
import { isScriptStepData, isLoopContainerData } from '@/models/nodes/types'
import { validateConnection } from '@/composables/useDependencyCheck'
import { ElMessage } from 'element-plus'

// Props
interface Props {
  /** The loop container node from the main graph whose children we display */
  containerNodeId: string
}

const props = defineProps<Props>()

const emit = defineEmits<{
  /** Request to exit sub-graph view and return to main graph */
  exit: []
}>()

// Injected from parent (index.vue)
const selectedNodeId = inject<Ref<string | null>>('selectedNodeId')
const graphInstance = inject<ShallowRef<Graph | null>>('graphInstance')

// Local state
const containerRef = ref<HTMLDivElement | null>(null)
let subGraph: Graph | null = null

// Quick edit state
const quickEditVisible = ref(false)
const quickEditNode = ref<Node | null>(null)
const quickEditPosition = ref<{ x: number; y: number } | null>(null)

// Script drop data interface (same as GraphContainer)
interface ScriptDropData {
  type: 'script'
  scriptId: string
  scriptName: string
  scriptVersion: string
  params: Array<{
    name: string
    type: string
    description?: string
    default?: unknown
    required?: boolean
  }>
}

/**
 * Get the main graph instance and the container node
 */
function getMainGraphAndContainer(): { mainGraph: Graph; containerNode: Node } | null {
  const mainGraph = graphInstance?.value
  if (!mainGraph) return null

  const cell = mainGraph.getCellById(props.containerNodeId)
  if (!cell || !cell.isNode()) return null

  return { mainGraph, containerNode: cell as Node }
}

/**
 * Extract child nodes from the loop container in the main graph
 * and populate the sub-graph with them.
 */
function populateSubGraph() {
  const result = getMainGraphAndContainer()
  if (!result) return

  const { mainGraph, containerNode } = result

  // Get all child nodes of the container
  const children = containerNode.getChildren()
  if (!children) return

  // Track node ID mapping (main graph ID -> sub-graph ID)
  // We use the same IDs so syncing is straightforward
  const childNodeIds = new Set<string>()
  const childEdgeIds = new Set<string>()

  // Add child nodes to sub-graph
  for (const child of children) {
    if (!child.isNode()) continue
    const childNode = child as Node
    childNodeIds.add(childNode.id)

    const data = childNode.getData() as NodeData
    const pos = childNode.position()

    // Determine shape based on data type
    const shape = isLoopContainerData(data) ? 'loop-container-node' : 'script-step-node'

    // Determine label
    let label = ''
    if (isScriptStepData(data)) {
      label = `${data.scriptName}\n${data.stepId.slice(0, 8)}`
    } else if (isLoopContainerData(data)) {
      label = `${data.loopType} loop\n${data.loopId.slice(0, 8)}`
    }

    // Build ports
    const ports: { items: Array<{ id: string; group: string }> } = { items: [] }
    const existingPorts = childNode.getPorts()
    for (const port of existingPorts) {
      ports.items.push({ id: port.id, group: port.group })
    }

    subGraph!.addNode({
      id: childNode.id,
      shape,
      x: pos.x,
      y: pos.y,
      label,
      data: { ...data },
      ports,
    })
  }

  // Add edges that connect child nodes within the container
  const allEdges = mainGraph.getEdges()
  for (const edge of allEdges) {
    const sourceCell = edge.getSourceCell()
    const targetCell = edge.getTargetCell()
    if (!sourceCell || !targetCell) continue

    // Only include edges where both endpoints are children of this container
    if (childNodeIds.has(sourceCell.id) && childNodeIds.has(targetCell.id)) {
      childEdgeIds.add(edge.id)

      const sourcePortId = (edge.getSource() as { port?: string })?.port
      const targetPortId = (edge.getTarget() as { port?: string })?.port

      subGraph!.addEdge({
        id: edge.id,
        source: sourcePortId
          ? { cell: sourceCell.id, port: sourcePortId }
          : { cell: sourceCell.id },
        target: targetPortId
          ? { cell: targetCell.id, port: targetPortId }
          : { cell: targetCell.id },
        attrs: edge.getAttrs(),
        data: edge.getData() ? { ...edge.getData() } : undefined,
      })
    }
  }

  // Auto-fit content
  if (subGraph!.getNodes().length > 0) {
    subGraph!.zoomToFit({ padding: 40, maxScale: 1 })
  }
}

/**
 * Sync changes from sub-graph back to the main graph's child nodes.
 * Called when exiting the sub-graph view.
 */
function syncBackToMainGraph() {
  const result = getMainGraphAndContainer()
  if (!result || !subGraph) return

  const { mainGraph, containerNode } = result

  // Get current children in the main graph
  const existingChildren = containerNode.getChildren() || []

  // Get all nodes in the sub-graph
  const subNodes = subGraph.getNodes()
  const subNodeIds = new Set(subNodes.map(n => n.id))

  // Update existing child nodes with new positions and data
  for (const subNode of subNodes) {
    const mainNode = mainGraph.getCellById(subNode.id)
    if (mainNode && mainNode.isNode()) {
      const mainNodeAsNode = mainNode as Node
      // Update position
      mainNodeAsNode.position(subNode.position())
      // Update data
      const subData = subNode.getData()
      if (subData) {
        mainNodeAsNode.setData(subData, { silent: true })
      }
      // Update label
      const label = subNode.getAttrByPath('text/text') || subNode.getLabels()
      if (typeof label === 'string') {
        mainNodeAsNode.setLabel(label)
      }
    }
  }

  // Remove children that were deleted in the sub-graph
  for (const child of existingChildren) {
    if (!subNodeIds.has(child.id)) {
      mainGraph.removeCell(child)
    }
  }

  // Add new children that were created in the sub-graph
  for (const subNode of subNodes) {
    const exists = mainGraph.getCellById(subNode.id)
    if (!exists) {
      // This node was created in the sub-graph — add to main graph and set parent
      const data = subNode.getData() as NodeData
      const pos = subNode.position()
      const shape = isLoopContainerData(data) ? 'loop-container-node' : 'script-step-node'

      let label = ''
      if (isScriptStepData(data)) {
        label = `${data.scriptName}\n${data.stepId.slice(0, 8)}`
      } else if (isLoopContainerData(data)) {
        label = `${data.loopType} loop\n${data.loopId.slice(0, 8)}`
      }

      const ports: { items: Array<{ id: string; group: string }> } = { items: [] }
      const existingPorts = subNode.getPorts()
      for (const port of existingPorts) {
        ports.items.push({ id: port.id, group: port.group })
      }

      const newNode = mainGraph.addNode({
        id: subNode.id,
        shape,
        x: pos.x,
        y: pos.y,
        label,
        data,
        ports,
      })

      // Set parent/child relationship
      newNode.setParent(containerNode)
      containerNode.addChild(newNode)
    }
  }

  // Sync edges within the container
  const subEdges = subGraph.getEdges()
  const subEdgeIds = new Set(subEdges.map(e => e.id))

  // Remove edges in main graph that no longer exist in sub-graph
  const mainEdges = mainGraph.getEdges()
  for (const mainEdge of mainEdges) {
    const sourceCell = mainEdge.getSourceCell()
    const targetCell = mainEdge.getTargetCell()
    if (!sourceCell || !targetCell) continue
    if (subNodeIds.has(sourceCell.id) && subNodeIds.has(targetCell.id)) {
      if (!subEdgeIds.has(mainEdge.id)) {
        mainGraph.removeCell(mainEdge)
      }
    }
  }

  // Add new edges from sub-graph
  for (const subEdge of subEdges) {
    const exists = mainGraph.getCellById(subEdge.id)
    if (!exists) {
      const sourceCell = subEdge.getSourceCell()
      const targetCell = subEdge.getTargetCell()
      if (!sourceCell || !targetCell) continue

      const sourcePortId = (subEdge.getSource() as { port?: string })?.port
      const targetPortId = (subEdge.getTarget() as { port?: string })?.port

      mainGraph.addEdge({
        id: subEdge.id,
        source: sourcePortId
          ? { cell: sourceCell.id, port: sourcePortId }
          : { cell: sourceCell.id },
        target: targetPortId
          ? { cell: targetCell.id, port: targetPortId }
          : { cell: targetCell.id },
        attrs: subEdge.getAttrs(),
        data: subEdge.getData() ? { ...subEdge.getData() } : undefined,
      })
    }
  }
}

onMounted(() => {
  if (!containerRef.value) return

  // Initialize sub-graph with similar config to main GraphContainer
  subGraph = new Graph({
    container: containerRef.value,
    width: containerRef.value.clientWidth,
    height: containerRef.value.clientHeight,
    grid: {
      visible: true,
      type: 'dot',
      size: 20,
      args: {
        color: '#e5e7eb',
        thickness: 1,
      },
    },
    panning: {
      enabled: true,
      modifiers: [],
    },
    zooming: {
      enabled: true,
      minScale: 0.25,
      maxScale: 2,
    },
    selecting: {
      enabled: true,
      multiple: true,
      rubberband: true,
      movable: true,
      showNodeSelectionBox: true,
    },
    connecting: {
      anchor: 'center',
      connectionPoint: 'anchor',
      snap: true,
      allowBlank: false,
      allowLoop: true,
      allowMulti: true,
      allowNode: true,
      allowEdge: false,
      validateConnection: ({ sourceCell, targetCell }) => {
        if (!sourceCell || !targetCell) return false
        const result = validateConnection(subGraph!, sourceCell.id, targetCell.id)
        if (!result.valid && result.error) {
          ElMessage.error(result.error)
        }
        return result.valid
      },
    },
    background: {
      color: '#f0f7ff', // Slightly different background to indicate sub-graph
    },
  })

  // Enable plugins
  subGraph.use(new History({ enabled: true }))
  subGraph.use(new Keyboard({ enabled: true, global: true }))
  subGraph.use(new Clipboard({ enabled: true }))
  subGraph.use(new Snapline({ enabled: true, sharp: true }))

  // Handle node selection
  subGraph.on('node:selected', ({ node }) => {
    if (selectedNodeId) {
      selectedNodeId.value = node.id
    }
  })

  subGraph.on('node:unselected', () => {
    if (selectedNodeId) {
      selectedNodeId.value = null
    }
  })

  // Handle node double-click
  subGraph.on('node:dblclick', ({ node, e }) => {
    const data = node.getData()
    // For loop containers inside sub-graph, we could support nested navigation
    // but for now, open quick edit for all nodes in sub-graph
    openQuickEdit(node, e)
  })

  // Handle blank area click
  subGraph.on('blank:click', () => {
    closeQuickEdit()
  })

  // Handle window resize
  const resizeObserver = new ResizeObserver(() => {
    if (containerRef.value && subGraph) {
      subGraph.resize(containerRef.value.clientWidth, containerRef.value.clientHeight)
    }
  })
  resizeObserver.observe(containerRef.value)
  ;(subGraph as Graph & { _resizeObserver?: ResizeObserver })._resizeObserver = resizeObserver

  // Setup drag and drop
  setupDragAndDrop()

  // Populate with child nodes from the main graph
  populateSubGraph()
})

onUnmounted(() => {
  // Sync changes back before destroying
  syncBackToMainGraph()

  const resizeObserver = (subGraph as Graph & { _resizeObserver?: ResizeObserver })?._resizeObserver
  if (resizeObserver) {
    resizeObserver.disconnect()
  }
  if (subGraph) {
    subGraph.dispose()
    subGraph = null
  }
})

/**
 * Setup drag and drop handlers for receiving script drops in sub-graph.
 * Dropped nodes become children of the loop container.
 */
function setupDragAndDrop() {
  const container = containerRef.value
  if (!container) return

  container.addEventListener('dragover', (event: DragEvent) => {
    event.preventDefault()
    if (event.dataTransfer) {
      event.dataTransfer.dropEffect = 'copy'
    }
  })

  container.addEventListener('drop', (event: DragEvent) => {
    event.preventDefault()

    if (!subGraph || !event.dataTransfer) return

    const jsonData = event.dataTransfer.getData('application/json')
    if (!jsonData) return

    try {
      const scriptData = JSON.parse(jsonData) as ScriptDropData

      if (scriptData.type === 'script') {
        const rect = container.getBoundingClientRect()
        const x = event.clientX - rect.left
        const y = event.clientY - rect.top

        const point = subGraph.clientToLocal(x, y)
        createScriptStepNode(scriptData, point.x, point.y)
      }
    } catch (err) {
      console.error('Failed to parse drop data in sub-graph:', err)
    }
  })
}

/**
 * Create a script step node in the sub-graph.
 * The parent/child relationship with the loop container will be set
 * when syncing back to the main graph.
 */
function createScriptStepNode(scriptData: ScriptDropData, x: number, y: number) {
  if (!subGraph) return

  const stepId = `step-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`

  const defaultParams: Record<string, unknown> = {}
  if (scriptData.params) {
    scriptData.params.forEach(param => {
      if (param.default !== undefined) {
        defaultParams[param.name] = param.default
      }
    })
  }

  const nodeData: ScriptStepData = {
    stepId,
    scriptName: scriptData.scriptName,
    scriptVersion: scriptData.scriptVersion,
    params: defaultParams,
    preconditions: [],
    resources: [],
    timeout: 30000,
    onFail: 'stop',
    exportOutputs: false,
    status: 'idle',
  }

  const node = subGraph.addNode({
    id: stepId,
    shape: 'script-step-node',
    x: x - 80,
    y: y - 32,
    label: `${scriptData.scriptName}\n${stepId.slice(0, 8)}`,
    data: nodeData,
    ports: {
      items: [
        { id: `input-${stepId}`, group: 'input' },
        { id: `output-${stepId}`, group: 'output' },
      ],
    },
  })

  subGraph.resetSelection([node])
  console.log('Created script step node in sub-graph:', scriptData.scriptName)
}

/**
 * Open quick edit form for a node in the sub-graph
 */
function openQuickEdit(node: Node, e: MouseEvent) {
  if (quickEditVisible.value) {
    closeQuickEdit()
  }

  const position = subGraph!.localToClient(node.position())
  const nodeBBox = node.getBBox()
  quickEditPosition.value = {
    x: position.x + nodeBBox.width + 20,
    y: position.y,
  }

  quickEditNode.value = node
  quickEditVisible.value = true
}

/**
 * Close quick edit form
 */
function closeQuickEdit() {
  quickEditVisible.value = false
  quickEditNode.value = null
  quickEditPosition.value = null
}

/**
 * Handle quick edit update
 */
function handleQuickEditUpdate(data: ScriptStepData) {
  if (!quickEditNode.value) return

  quickEditNode.value.setData(data, { silent: false })
  const label = `${data.scriptName}\n${data.stepId.slice(0, 8)}`
  quickEditNode.value.setLabel(label)

  console.log('Updated node data in sub-graph:', data.stepId)
}

// Expose for parent
defineExpose({
  subGraph,
  syncBackToMainGraph,
})
</script>

<template>
  <div ref="containerRef" class="sub-graph-container">
    <!-- Sub-graph indicator overlay -->
    <div class="sub-graph-indicator">
      <span class="indicator-icon">&#x21A9;</span>
      <span class="indicator-text">Sub-graph view</span>
    </div>
  </div>
</template>

<style scoped>
.sub-graph-container {
  width: 100%;
  height: 100%;
  min-height: 400px;
  background: #f0f7ff;
  position: relative;
}

.sub-graph-indicator {
  position: absolute;
  top: 8px;
  right: 8px;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  background: rgba(59, 130, 246, 0.1);
  border: 1px solid rgba(59, 130, 246, 0.2);
  border-radius: 4px;
  font-size: 11px;
  color: #3b82f6;
  pointer-events: none;
  z-index: 10;
}

.indicator-icon {
  font-size: 14px;
}

.indicator-text {
  font-weight: 500;
}
</style>
