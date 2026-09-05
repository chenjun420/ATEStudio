<script setup lang="ts">
/**
 * SubGraphContainer - Renders a sub-graph view for a loop container's child nodes.
 * Creates its own X6 Graph instance with its own coordinate system.
 * Child nodes are extracted from the parent loop container node in the main graph.
 * Changes are synced back to the main graph when exiting the sub-graph view.
 */
import { onMounted, onUnmounted, ref, inject, watch, computed } from 'vue'
import { Graph, History, Keyboard, Clipboard, Snapline, Selection } from '@antv/x6'
import type { Ref, ShallowRef } from 'vue'
import type { Node } from '@antv/x6'
import type { ScriptStepData, NodeData } from '@/models/nodes/types'
import { isScriptStepData, isLoopContainerData } from '@/models/nodes/types'
import { validateConnection } from '@/composables/useDependencyCheck'
import { useNodeBreakpoints, NODE_BREAKPOINTS_KEY } from '@/composables/useNodeBreakpoints'
import { setBreakpointBadge, setBreakpointHitHalo } from '../breakpointMarkers'
import { ElMessage } from 'element-plus'

// Props
interface Props {
  /** The loop container node from the main graph whose children we display */
  containerNodeId: string
  /** Active execution run ID (enables breakpoint toggles; empty = not running) */
  runId?: string
}

const props = defineProps<Props>()

const emit = defineEmits<{
  /** Request to exit sub-graph view and return to main graph */
  exit: []
}>()

// Injected from parent (index.vue)
const selectedNodeId = inject<Ref<string | null>>('selectedNodeId')
const graphInstance = inject<ShallowRef<Graph | null>>('graphInstance')

// Shared step-breakpoint composable (task 23) — injected from index.vue;
// fallback local instance keeps the component mountable standalone/tests.
const breakpoints =
  inject<ReturnType<typeof useNodeBreakpoints> | null>(NODE_BREAKPOINTS_KEY, null) ??
  useNodeBreakpoints((m, t) =>
    t === 'success'
      ? ElMessage.success(m)
      : t === 'warning'
        ? ElMessage.warning(m)
        : t === 'error'
          ? ElMessage.error(m)
          : ElMessage.info(m),
  )

// Local state
const containerRef = ref<HTMLDivElement | null>(null)
let subGraph: Graph | null = null

// Quick edit state
const quickEditVisible = ref(false)
const quickEditNode = ref<Node | null>(null)
const quickEditPosition = ref<{ x: number; y: number } | null>(null)

// ============================================
// Step Breakpoint Integration (task 23)
// ============================================

const breakpointMenuVisible = ref(false)
const breakpointMenuPosition = ref({ x: 0, y: 0 })
const breakpointMenuData = ref<ScriptStepData | null>(null)

/** Paint breakpoint badge + hit halo for every script-step node in the sub-graph. */
function refreshBreakpointMarkers() {
  if (!subGraph) return
  for (const node of subGraph.getNodes()) {
    const data = node.getData() as NodeData
    if (!isScriptStepData(data)) continue
    setBreakpointBadge(node, breakpoints.isArmed(data.stepId))
    setBreakpointHitHalo(node, breakpoints.hitStep.value === data.stepId)
  }
}

// Repaint when the shared armed set / hit target changes.
watch(
  () => ({ ...breakpoints.armedSteps, hit: breakpoints.hitStep.value }),
  () => refreshBreakpointMarkers(),
  { deep: true },
)

function showBreakpointMenu(e: MenuTriggerEvent, data: ScriptStepData) {
  breakpointMenuPosition.value = { x: e.clientX, y: e.clientY }
  breakpointMenuData.value = data
  breakpointMenuVisible.value = true
}

function closeBreakpointMenu() {
  breakpointMenuVisible.value = false
  breakpointMenuData.value = null
}

const breakpointMenuHasBreakpoint = computed(() =>
  breakpointMenuData.value ? breakpoints.isArmed(breakpointMenuData.value.stepId) : false,
)

async function handleToggleBreakpoint() {
  const data = breakpointMenuData.value
  closeBreakpointMenu()
  if (!data || !props.runId) {
    ElMessage.warning('请先启动一次仿真运行（断点属于运行），再为步骤设置断点')
    return
  }
  await breakpoints.toggleStep(props.runId, data.stepId)
  refreshBreakpointMarkers()
}

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
 * Minimal shape of the X6 context-menu DOM event we rely on (clientX/Y +
 * preventDefault). Avoids coupling to @antv/x6 DOM event types.
 */
interface MenuTriggerEvent {
  clientX: number
  clientY: number
  preventDefault: () => void
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

    // Build ports (X6 PortMetadata.group is optional)
    const ports: { items: Array<{ id: string; group?: string }> } = { items: [] }
    const existingPorts = childNode.getPorts()
    for (const port of existingPorts) {
      if (!port.id) continue
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
      const subPos = subNode.position()
      mainNodeAsNode.position(subPos.x, subPos.y)
      // Update data
      const subData = subNode.getData()
      if (subData) {
        mainNodeAsNode.setData(subData, { silent: true })
      }
      // Update label via X6 attrs (X6 Node has no getLabels/setLabel)
      const label = subNode.getAttrByPath<string>('label/text')
      if (typeof label === 'string') {
        mainNodeAsNode.attr('label/text', label)
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

      const ports: { items: Array<{ id: string; group?: string }> } = { items: [] }
      const existingPorts = subNode.getPorts()
      for (const port of existingPorts) {
        if (!port.id) continue
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
    // X6 3.x configures wheel-zoom via the `mousewheel` option (no `zooming`
    // Graph option exists); plain wheel zooms the sub-graph canvas.
    mousewheel: {
      enabled: true,
      minScale: 0.25,
      maxScale: 2,
      modifiers: [],
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

  // Enable plugins. X6 3.x: selection is a plugin (the v2 `selecting`
  // Graph option no longer exists).
  subGraph.use(new Selection({
    enabled: true,
    multiple: true,
    rubberband: true,
    movable: true,
    showNodeSelectionBox: true,
  }))
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
  subGraph.on('node:dblclick', ({ node }) => {
    // For loop containers inside sub-graph, we could support nested navigation
    // but for now, open quick edit for all nodes in sub-graph
    openQuickEdit(node)
  })

  // Right-click context menu on script step nodes (task 23: breakpoint toggle)
  subGraph.on('node:contextmenu', ({ node, e }) => {
    const data = node.getData() as NodeData
    if (isScriptStepData(data)) {
      e.preventDefault()
      showBreakpointMenu(e as MenuTriggerEvent, data)
    }
  })

  // Paint breakpoint markers on nodes added after populate.
  subGraph.on('node:added', ({ node }) => {
    const data = node.getData() as NodeData
    if (!isScriptStepData(data)) return
    setBreakpointBadge(node, breakpoints.isArmed(data.stepId))
    setBreakpointHitHalo(node, breakpoints.hitStep.value === data.stepId)
  })

  // Handle blank area click
  subGraph.on('blank:click', () => {
    closeQuickEdit()
    closeBreakpointMenu()
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

  // Initial breakpoint marker paint.
  refreshBreakpointMarkers()
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
function openQuickEdit(node: Node) {
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

// Expose for parent
defineExpose({
  subGraph,
  syncBackToMainGraph,
})
</script>

<template>
  <div ref="containerRef" class="sub-graph-container" @click="closeBreakpointMenu">
    <!-- Sub-graph indicator overlay -->
    <div class="sub-graph-indicator">
      <span class="indicator-icon">&#x21A9;</span>
      <span class="indicator-text">Sub-graph view</span>
    </div>

    <!-- Breakpoint context menu (task 23) -->
    <Teleport to="body">
      <div
        v-if="breakpointMenuVisible"
        class="bp-context-menu"
        :style="{ left: `${breakpointMenuPosition.x}px`, top: `${breakpointMenuPosition.y}px` }"
        @click.stop
      >
        <button
          class="bp-context-menu-item"
          data-testid="subgraph-context-menu-toggle-breakpoint"
          :disabled="breakpoints.busy.value"
          @click="handleToggleBreakpoint"
        >
          <svg class="tw-w-4 tw-h-4" fill="currentColor" viewBox="0 0 24 24">
            <circle cx="12" cy="12" r="6" />
          </svg>
          {{ breakpointMenuHasBreakpoint ? '移除断点' : '切换断点' }}
        </button>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.sub-graph-container {
  width: 100%;
  height: 100%;
  min-height: 400px;
  background: var(--color-bg-secondary);
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
  background: var(--color-bg-tertiary);
  border: 1px solid var(--color-border-default);
  border-radius: 4px;
  font-size: 11px;
  color: var(--color-primary);
  pointer-events: none;
  z-index: 10;
}

.indicator-icon {
  font-size: 14px;
}

.indicator-text {
  font-weight: 500;
}

/* Breakpoint context menu (mirrors GraphContainer's .context-menu styles) */
.bp-context-menu {
  position: fixed;
  z-index: 1000;
  min-width: 160px;
  background: var(--color-bg-elevated);
  border: 1px solid var(--color-border-default);
  border-radius: 8px;
  box-shadow: var(--shadow-lg);
  padding: 4px;
}

.bp-context-menu-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 12px;
  font-size: 13px;
  color: var(--color-text-primary);
  background: none;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  transition: background-color 150ms ease;
}

.bp-context-menu-item:hover:not(:disabled) {
  background: var(--color-bg-tertiary);
  color: var(--color-primary);
}

.bp-context-menu-item:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
