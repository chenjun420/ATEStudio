<script setup lang="ts">
import { onMounted, onUnmounted, ref, shallowRef, inject, watch, computed } from 'vue'
import { Graph, History, Keyboard, Clipboard, Snapline, Selection } from '@antv/x6'
import type { Ref, ShallowRef } from 'vue'
import type { Node } from '@antv/x6'
import type { ScriptStepData, NodeData } from '@/models/nodes/types'
import { isLoopContainerData, isScriptStepData } from '@/models/nodes/types'
import { validateConnection } from '@/composables/useDependencyCheck'
import { useExecutionStatus, type StepStatus } from '@/composables/useExecutionStatus'
import {
  useNodeBreakpoints,
  NODE_BREAKPOINTS_KEY,
  type UseNodeBreakpointsReturn,
} from '@/composables/useNodeBreakpoints'
import { setBreakpointBadge, setBreakpointHitHalo } from '../breakpointMarkers'
import { searchScripts } from '@/api/scripts'
import { ElMessage } from 'element-plus'
import NodeQuickEdit from './NodeQuickEdit.vue'

// Props
const props = defineProps<{
  /** Current execution run ID — when set, SSE connection is established for status updates */
  runId?: string
}>()

// Emit events for sub-graph navigation and script editing
const emit = defineEmits<{
  /** Emitted when a loop-container-node is double-clicked to enter sub-graph view */
  'enter-sub-graph': [containerNodeId: string]
  /** Emitted when user wants to open the full script editor dialog */
  'edit-script': [payload: { scriptId: string; scriptName: string }]
}>()

// Props for container dimensions
const containerRef = ref<HTMLDivElement | null>(null)
const selectedNodeId = inject<Ref<string | null>>('selectedNodeId')
const graphInstance = inject<ShallowRef<Graph | null>>('graphInstance')

// Quick edit state. Use shallowRef: X6 Node instances are non-reactive
// objects with protected members that Vue's deep ref unwrapping would strip.
const quickEditVisible = ref(false)
const quickEditNode = shallowRef<Node | null>(null)
const quickEditPosition = ref<{ x: number; y: number } | null>(null)

// Graph instance reference
let graph: Graph | null = null

// ============================================
// Execution Status Integration
// ============================================

// Reactive runId ref for the composable
const runIdRef = computed(() => props.runId ?? '')

// Use the execution status composable
const { stepStatuses, setTotalSteps, reset: resetExecution } = useExecutionStatus(runIdRef)

// ============================================
// Step Breakpoint Integration (task 23)
// ============================================

// Shared step-breakpoint composable, provided by index.vue so the main graph
// and the sub-graph render identical markers. Fallback: local instance.
const breakpoints =
  inject<UseNodeBreakpointsReturn | null>(NODE_BREAKPOINTS_KEY, null) ??
  useNodeBreakpoints((m, t) =>
    t === 'success'
      ? ElMessage.success(m)
      : t === 'warning'
        ? ElMessage.warning(m)
        : t === 'error'
          ? ElMessage.error(m)
          : ElMessage.info(m),
  )

/** Paint breakpoint badge + hit halo for every script-step node in the graph. */
function refreshBreakpointMarkers() {
  if (!graph) return
  for (const node of graph.getNodes()) {
    const data = node.getData() as NodeData
    if (!isScriptStepData(data)) continue
    setBreakpointBadge(node, breakpoints.isArmed(data.stepId))
    setBreakpointHitHalo(node, breakpoints.hitStep.value === data.stepId)
  }
}

// Repaint markers whenever armed set or hit target changes.
watch(
  () => ({ ...breakpoints.armedSteps, hit: breakpoints.hitStep.value }),
  () => refreshBreakpointMarkers(),
  { deep: true },
)

// Load durable breakpoints and open the BREAKPOINT_HIT stream for a run.
watch(
  () => props.runId,
  (newRunId) => {
    if (newRunId) {
      void breakpoints.load(newRunId)
      breakpoints.connect(newRunId)
    } else {
      breakpoints.disconnect()
      breakpoints.clearHit()
    }
  },
)

/**
 * Status → visual attribute mapping for node styling
 */
const STATUS_VISUAL_MAP: Record<string, { stroke: string; fill: string; strokeWidth: number }> = {
  idle: { stroke: '#d1d5db', fill: '#ffffff', strokeWidth: 1 },
  running: { stroke: '#3b82f6', fill: '#eff6ff', strokeWidth: 2 },
  passed: { stroke: '#22c55e', fill: '#f0fdf4', strokeWidth: 2 },
  failed: { stroke: '#ef4444', fill: '#fef2f2', strokeWidth: 2 },
  error: { stroke: '#f97316', fill: '#fff7ed', strokeWidth: 2 },
  skipped: { stroke: '#9ca3af', fill: '#f9fafb', strokeWidth: 1 },
}

/**
 * Apply a step status to a node's visual appearance.
 * Updates the node's body attrs (border color, fill, stroke width)
 * and stores the status in the node's data.
 */
function applyStepNodeStatus(node: Node, status: StepStatus) {
  const visual = STATUS_VISUAL_MAP[status] ?? STATUS_VISUAL_MAP.idle

  // Update node visual attrs
  node.setAttrs({
    body: {
      stroke: visual.stroke,
      fill: visual.fill,
      strokeWidth: visual.strokeWidth,
    },
  })

  // Update node data status
  const data = node.getData<NodeData>()
  if (isScriptStepData(data) || isLoopContainerData(data)) {
    node.setData({ ...data, status }, { silent: true })
  }
}

/**
 * Reset all node visual states back to idle.
 */
function resetAllNodeVisuals() {
  if (!graph) return
  const nodes = graph.getNodes()
  for (const node of nodes) {
    const data = node.getData() as NodeData
    if (isScriptStepData(data) || isLoopContainerData(data)) {
      applyStepNodeStatus(node, 'idle')
    }
  }
}

// Watch stepStatuses and update corresponding node visuals
watch(stepStatuses, (statuses) => {
  if (!graph) return

  for (const [stepId, status] of Object.entries(statuses)) {
    // Try to find the node by stepId (stored in node data)
    const nodes = graph.getNodes()
    for (const node of nodes) {
      const data = node.getData() as NodeData
      if (isScriptStepData(data) && data.stepId === stepId) {
        applyStepNodeStatus(node, status)
        break
      }
      if (isLoopContainerData(data) && data.loopId === stepId) {
        applyStepNodeStatus(node, status)
        break
      }
    }
  }
}, { deep: true })

// Watch runId to set total steps when execution starts
watch(() => props.runId, (newRunId) => {
  if (newRunId && graph) {
    // Count script-step nodes for progress tracking
    const stepNodes = graph.getNodes().filter(node => {
      const data = node.getData() as NodeData
      return isScriptStepData(data)
    })
    setTotalSteps(stepNodes.length)
  }
  if (!newRunId) {
    resetAllNodeVisuals()
  }
})

// Script drop data interface
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

onMounted(() => {
  if (!containerRef.value) return

  // Initialize X6 Graph
  graph = new Graph({
    container: containerRef.value,
    width: containerRef.value.clientWidth,
    height: containerRef.value.clientHeight,
    // Grid configuration
    grid: {
      visible: true,
      type: 'dot',
      size: 20,
      args: {
        color: '#e5e7eb',
        thickness: 1,
      },
    },
    // Panning and zooming. X6 3.x configures wheel-zoom via the `mousewheel`
    // option (there is no `zooming` Graph option); plain wheel zooms the canvas.
    panning: {
      enabled: true,
      modifiers: [],
    },
    mousewheel: {
      enabled: true,
      minScale: 0.25,
      maxScale: 2,
      modifiers: [],
    },
    // Connection settings
    connecting: {
      anchor: 'center',
      connectionPoint: 'anchor',
      snap: true,
      allowBlank: false,
      allowLoop: true,
      allowMulti: true,
      allowNode: true,
      allowEdge: false,
      // Validate connection to prevent circular dependencies
      // Back-edges within the same loop container are allowed
      validateConnection: ({ sourceCell, targetCell }) => {
        if (!sourceCell || !targetCell) return false
        
        const sourceId = sourceCell.id
        const targetId = targetCell.id
        
        // Determine container context: if both nodes share the same parent container,
        // pass that containerId to allow scoped back-edges within loops
        const sourceParent = sourceCell.getParent()
        const targetParent = targetCell.getParent()
        const containerId = (sourceParent && targetParent && sourceParent.id === targetParent.id)
          ? sourceParent.id
          : undefined
        
        const result = validateConnection(graph!, sourceId, targetId, containerId)
        
        if (!result.valid && result.error) {
          ElMessage.error(result.error)
        }
        
        return result.valid
      },
    },
    // Background
    background: {
      color: '#f9fafb',
    },
  })

  // Enable plugins for selection, history, keyboard, clipboard, snapline
  // X6 3.x: selection is a plugin (the v2 `selecting` Graph option no longer exists)
  graph.use(new Selection({
    enabled: true,
    multiple: true,
    rubberband: true,
    movable: true,
    showNodeSelectionBox: true,
  }))
  graph.use(new History({ enabled: true }))
  graph.use(new Keyboard({ enabled: true, global: true }))
  graph.use(new Clipboard({ enabled: true }))
  graph.use(new Snapline({ enabled: true, sharp: true }))

  // Handle node selection
  graph.on('node:selected', ({ node }) => {
    if (selectedNodeId) {
      selectedNodeId.value = node.id
    }
  })

  graph.on('node:unselected', () => {
    if (selectedNodeId) {
      selectedNodeId.value = null
    }
  })

  // Handle node double-click: enter sub-graph for loop containers, quick edit for others
  graph.on('node:dblclick', ({ node }) => {
    const data = node.getData() as NodeData
    if (isLoopContainerData(data)) {
      // Enter sub-graph view for loop container
      emit('enter-sub-graph', node.id)
    } else {
      // Open quick edit for non-loop nodes
      openQuickEdit(node)
    }
  })

  // Handle blank area click to close quick edit
  graph.on('blank:click', () => {
    closeQuickEdit()
  })

  // Handle right-click context menu on script step nodes
  graph.on('node:contextmenu', ({ node, e }) => {
    const data = node.getData() as NodeData
    if (isScriptStepData(data)) {
      e.preventDefault()
      showContextMenu(e, data)
    }
  })

  // Paint breakpoint markers on nodes added after mount (drag-drop etc.).
  graph.on('node:added', ({ node }) => {
    const data = node.getData() as NodeData
    if (!isScriptStepData(data)) return
    setBreakpointBadge(node, breakpoints.isArmed(data.stepId))
    setBreakpointHitHalo(node, breakpoints.hitStep.value === data.stepId)
  })

  // Store graph instance for property panel
  if (graphInstance) {
    graphInstance.value = graph
  }

  // Handle window resize
  const resizeObserver = new ResizeObserver(() => {
    if (containerRef.value && graph) {
      graph.resize(containerRef.value.clientWidth, containerRef.value.clientHeight)
    }
  })
  resizeObserver.observe(containerRef.value)

  // Store resize observer for cleanup
  ;(graph as Graph & { _resizeObserver?: ResizeObserver })._resizeObserver = resizeObserver

  // Setup drag and drop handlers on container
  setupDragAndDrop()

  // Add demo nodes for testing
  addDemoNodes()

  // Initial breakpoint marker paint (run may already carry armed breakpoints).
  refreshBreakpointMarkers()
})

onUnmounted(() => {
  const resizeObserver = (graph as Graph & { _resizeObserver?: ResizeObserver })?._resizeObserver
  if (resizeObserver) {
    resizeObserver.disconnect()
  }
  if (graphInstance) {
    graphInstance.value = null
  }
  resetExecution()
  if (graph) {
    graph.dispose()
    graph = null
  }
})

/**
 * Setup drag and drop handlers for receiving script drops
 */
function setupDragAndDrop() {
  const container = containerRef.value
  if (!container) return

  // Prevent default drag over behavior to allow drop
  container.addEventListener('dragover', (event: DragEvent) => {
    event.preventDefault()
    if (event.dataTransfer) {
      event.dataTransfer.dropEffect = 'copy'
    }
  })

  // Handle drop event
  container.addEventListener('drop', (event: DragEvent) => {
    event.preventDefault()
    
    if (!graph || !event.dataTransfer) return

    // Try to parse script data from dataTransfer
    const jsonData = event.dataTransfer.getData('application/json')
    if (!jsonData) return

    try {
      const scriptData = JSON.parse(jsonData) as ScriptDropData
      
      if (scriptData.type === 'script') {
        // Calculate drop position relative to graph
        const rect = container.getBoundingClientRect()
        const x = event.clientX - rect.left
        const y = event.clientY - rect.top
        
        // Convert to graph coordinates (account for pan/zoom)
        const point = graph.clientToLocal(x, y)
        
        // Create the script step node
        createScriptStepNode(scriptData, point.x, point.y)
      }
    } catch (err) {
      console.error('Failed to parse drop data:', err)
    }
  })
}

/**
 * Create a ScriptStepNode at the specified position
 */
function createScriptStepNode(scriptData: ScriptDropData, x: number, y: number) {
  if (!graph) return

  // Generate unique step ID
  const stepId = `step-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`
  
  // Build default params from script parameter definitions
  const defaultParams: Record<string, unknown> = {}
  if (scriptData.params) {
    scriptData.params.forEach(param => {
      if (param.default !== undefined) {
        defaultParams[param.name] = param.default
      }
    })
  }

  // Create node data
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

  // Add node to graph
  const node = graph.addNode({
    id: stepId,
    shape: 'script-step-node',
    x: x - 80, // Center on drop point
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

  // Select the newly created node
  graph.resetSelection([node])
  
  console.log('Created script step node:', scriptData.scriptName, 'at', x, y)
}

/**
 * Add demo nodes for initial testing
 */
function addDemoNodes() {
  if (!graph) return

  // Script Step Node - demonstrating the new professional node style
  graph.addNode({
    id: 'script-step-1',
    shape: 'script-step-node',
    x: 100,
    y: 80,
    label: 'Initialize\nstep-001',
    data: {
      stepId: 'step-001',
      scriptName: 'initialize',
      scriptVersion: '1.0.0',
      params: { channel: 'CH1' },
      preconditions: [],
      resources: [],
      timeout: 30000,
      onFail: 'stop',
      exportOutputs: false,
      status: 'idle',
    },
    ports: {
      items: [
        { id: 'out-1', group: 'output' },
      ],
    },
  })

  // Script Step Node - Running state
  graph.addNode({
    id: 'script-step-2',
    shape: 'script-step-node',
    x: 320,
    y: 80,
    label: 'Processing\nstep-002',
    attrs: {
      body: {
        stroke: '#3b82f6',
      },
    },
    ports: {
      items: [
        { id: 'in-2', group: 'input' },
        { id: 'out-2', group: 'output' },
      ],
    },
  })

  // Script Step Node - Passed state
  graph.addNode({
    id: 'script-step-3',
    shape: 'script-step-node',
    x: 540,
    y: 80,
    label: 'Validate\nstep-003',
    attrs: {
      body: {
        stroke: '#10b981',
      },
    },
    ports: {
      items: [
        { id: 'in-3', group: 'input' },
      ],
    },
  })

  // Script Step Node - Failed state
  graph.addNode({
    id: 'script-step-4',
    shape: 'script-step-node',
    x: 320,
    y: 200,
    label: 'Check Data\nstep-004',
    attrs: {
      body: {
        stroke: '#ef4444',
      },
    },
    ports: {
      items: [
        { id: 'in-4', group: 'input' },
        { id: 'out-4', group: 'output' },
      ],
    },
  })

  // Script Step Node - Error state
  graph.addNode({
    id: 'script-step-5',
    shape: 'script-step-node',
    x: 540,
    y: 200,
    label: 'Cleanup\nstep-005',
    attrs: {
      body: {
        stroke: '#f59e0b',
      },
    },
    ports: {
      items: [
        { id: 'in-5', group: 'input' },
      ],
    },
  })

  // Connect nodes
  graph.addEdge({
    source: { cell: 'script-step-1', port: 'out-1' },
    target: { cell: 'script-step-2', port: 'in-2' },
    attrs: {
      line: {
        stroke: '#6b7280',
        strokeWidth: 2,
      },
    },
  })

  graph.addEdge({
    source: { cell: 'script-step-2', port: 'out-2' },
    target: { cell: 'script-step-3', port: 'in-3' },
    attrs: {
      line: {
        stroke: '#6b7280',
        strokeWidth: 2,
      },
    },
  })

  graph.addEdge({
    source: { cell: 'script-step-2', port: 'out-2' },
    target: { cell: 'script-step-4', port: 'in-4' },
    attrs: {
      line: {
        stroke: '#ef4444',
        strokeWidth: 2,
      },
    },
  })

  graph.addEdge({
    source: { cell: 'script-step-4', port: 'out-4' },
    target: { cell: 'script-step-5', port: 'in-5' },
    attrs: {
      line: {
        stroke: '#f59e0b',
        strokeWidth: 2,
      },
    },
  })
}

// Expose graph for parent components
defineExpose({
  graph,
  resetAllNodeVisuals,
  setTotalSteps,
})

/**
 * Open quick edit form for a node
 */
function openQuickEdit(node: Node) {
  // Close if already open
  if (quickEditVisible.value) {
    closeQuickEdit()
  }

  // Get node position in screen coordinates
  const position = graph!.localToClient(node.position())
  
  // Calculate position offset to show form near the node
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
  
  // Update node data
  quickEditNode.value.setData(data, { silent: false })

  // Update node label via X6 attrs (X6 Node has no setLabel method)
  const label = `${data.scriptName}\n${data.stepId.slice(0, 8)}`
  quickEditNode.value.attr('label/text', label)

  console.log('Updated node data:', data.stepId)
}

// ============================================
// Context Menu State
// ============================================

const contextMenuVisible = ref(false)
const contextMenuPosition = ref({ x: 0, y: 0 })
const contextMenuScriptData = ref<ScriptStepData | null>(null)

/**
 * Show context menu for a script step node
 */
function showContextMenu(e: MenuTriggerEvent, data: ScriptStepData) {
  contextMenuPosition.value = { x: e.clientX, y: e.clientY }
  contextMenuScriptData.value = data
  contextMenuVisible.value = true
}

/**
 * Close context menu
 */
function closeContextMenu() {
  contextMenuVisible.value = false
  contextMenuScriptData.value = null
}

/** Whether the node backing the open context menu has an armed breakpoint. */
const contextMenuHasBreakpoint = computed(() =>
  contextMenuScriptData.value ? breakpoints.isArmed(contextMenuScriptData.value.stepId) : false,
)

/**
 * Handle "Toggle breakpoint" context menu action (task 23).
 * Creates/deletes a typed `step` breakpoint via the shared breakpoint
 * composable (same API + ticketed SSE stream as the SimulationConsole).
 */
async function handleContextMenuToggleBreakpoint() {
  const data = contextMenuScriptData.value
  closeContextMenu()
  if (!data || !props.runId) {
    ElMessage.warning('请先启动一次仿真运行（断点属于运行），再为步骤设置断点')
    return
  }
  await breakpoints.toggleStep(props.runId, data.stepId)
  refreshBreakpointMarkers()
}

/**
 * Handle "Edit Script" context menu action
 */
async function handleContextMenuEditScript() {
  const data = contextMenuScriptData.value
  closeContextMenu()
  
  if (!data?.scriptName) return

  try {
    // Resolve script ID from script name
    const scripts = await searchScripts(data.scriptName)
    const matchedScript = scripts.find(s => s.name === data.scriptName)
    
    if (matchedScript) {
      emit('edit-script', { scriptId: matchedScript.id, scriptName: data.scriptName })
    } else {
      console.warn(`Script "${data.scriptName}" not found in registry`)
    }
  } catch (err) {
    console.error('Failed to resolve script ID:', err)
  }
}
</script>

<template>
  <div ref="containerRef" class="graph-container" @click="closeContextMenu">
    <!-- Quick Edit Form -->
    <NodeQuickEdit
      :node="quickEditNode"
      :position="quickEditPosition"
      :visible="quickEditVisible"
      @close="closeQuickEdit"
      @update="handleQuickEditUpdate"
    />

    <!-- Context Menu for Script Step Nodes -->
    <Teleport to="body">
      <div
        v-if="contextMenuVisible"
        class="context-menu"
        :style="{ left: `${contextMenuPosition.x}px`, top: `${contextMenuPosition.y}px` }"
        @click.stop
      >
        <button class="context-menu-item" @click="handleContextMenuEditScript">
          <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4" />
          </svg>
          Edit Script
        </button>
        <button
          class="context-menu-item"
          data-testid="context-menu-toggle-breakpoint"
          :disabled="breakpoints.busy.value"
          @click="handleContextMenuToggleBreakpoint"
        >
          <svg class="tw-w-4 tw-h-4" fill="currentColor" viewBox="0 0 24 24">
            <circle cx="12" cy="12" r="6" />
          </svg>
          {{ contextMenuHasBreakpoint ? '移除断点' : '切换断点' }}
        </button>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.graph-container {
  width: 100%;
  height: 100%;
  min-height: 400px;
  background: var(--color-bg-secondary);
  position: relative;
}

.context-menu {
  position: fixed;
  z-index: 1000;
  min-width: 160px;
  background: var(--color-bg-elevated);
  border: 1px solid var(--color-border-default);
  border-radius: 8px;
  box-shadow: var(--shadow-lg);
  padding: 4px;
}

.context-menu-item {
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

.context-menu-item:hover {
  background: var(--color-bg-tertiary);
  color: var(--color-primary);
}
</style>