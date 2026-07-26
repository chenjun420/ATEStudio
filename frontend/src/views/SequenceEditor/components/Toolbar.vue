<script setup lang="ts">
import { ref, inject, computed, watch, onMounted, onUnmounted } from 'vue'
import type { Graph } from '@antv/x6'
import { History } from '@antv/x6'
import type { ShallowRef } from 'vue'
import { useSerializer } from '@/composables/useSerializer'
import { useExecutionStatus } from '@/composables/useExecutionStatus'
import { ElMessage, ElSelect, ElOption, ElButton } from 'element-plus'
import { fetchSequences, createSequence, updateSequence, type Sequence } from '@/api/sequences'
import { createExecution, abortExecution } from '@/api/executions'

// Emit events for sequence selection and execution status
const emit = defineEmits<{
  sequenceSelected: [sequence: Sequence]
  sequenceCreated: [sequence: Sequence]
  /** Emitted when a new execution run starts — parent should pass runId to GraphContainer */
  executionStarted: [runId: string]
  /** Emitted when execution ends (completed/failed/aborted) */
  executionEnded: []
}>()

// Inject current sequence being edited
const currentSequence = inject<ShallowRef<Sequence | null>>('currentSequence')

// Inject graph instance from parent
const graphInstance = inject<ShallowRef<Graph | null>>('graphInstance')

// Sequence management state
const sequences = ref<Sequence[]>([])
const selectedSequenceId = ref<string>('')
const isLoadingSequences = ref(false)
const sequenceError = ref<string | null>(null)

// Use serializer composable
const { exportGraphToYaml, importYamlToGraph, graphToYaml } = useSerializer()

// ============================================
// Auto-Layout Toggle
// ============================================

/** Whether dagre auto-layout is enabled for YAML imports. Default: true (on). */
const autoLayoutEnabled = ref(true)

// ============================================
// Execution Status
// ============================================

// Current execution run ID
const currentRunId = ref('')

// Use execution status composable
const { stepStatuses, executionStatus, isRunning, progressText, reset: resetExecution } = useExecutionStatus(currentRunId)

// Execution loading state (for button spinner)
const isStartingExecution = ref(false)
const isAbortingExecution = ref(false)

// Zoom state
const zoomLevel = ref(100)

// History state (for button disabled state)
const canUndo = ref(false)
const canRedo = ref(false)

// Update history state periodically
let historyInterval: ReturnType<typeof setInterval> | null = null

function updateHistoryState() {
  if (!graphInstance?.value) return
  
  const history = graphInstance.value.getPlugin('history') as History | undefined
  if (history) {
    canUndo.value = history.canUndo()
    canRedo.value = history.canRedo()
  }
}

// === Sequence Management ===

async function loadSequences() {
  isLoadingSequences.value = true
  sequenceError.value = null
  
  try {
    sequences.value = await fetchSequences()
  } catch (error) {
    sequenceError.value = 'Failed to load sequences'
    console.error('Error loading sequences:', error)
    ElMessage.error('Failed to load sequences from server')
  } finally {
    isLoadingSequences.value = false
  }
}

async function handleNewSequence() {
  const timestamp = new Date().toISOString().slice(0, 19).replace('T', ' ')
  const defaultSequence = {
    name: `New Sequence ${sequences.value.length + 1}`,
    description: 'A new test sequence',
    version: '1.0.0',
    yaml_content: 'steps:\n  []\n',
  }
  
  try {
    const newSequence = await createSequence(defaultSequence)
    sequences.value.push(newSequence)
    selectedSequenceId.value = newSequence.id
    emit('sequenceCreated', newSequence)
    ElMessage.success(`Created "${newSequence.name}"`)
  } catch (error) {
    console.error('Error creating sequence:', error)
    ElMessage.error('Failed to create new sequence')
  }
}

function handleSequenceChange(sequenceId: string) {
  const sequence = sequences.value.find(s => s.id === sequenceId)
  if (sequence) {
    emit('sequenceSelected', sequence)
  }
}

onMounted(() => {
  historyInterval = setInterval(updateHistoryState, 200)
  loadSequences()
})

onUnmounted(() => {
  if (historyInterval) {
    clearInterval(historyInterval)
  }
})

// Computed zoom display
const zoomDisplay = computed(() => `${Math.round(zoomLevel.value)}%`)

// === Execution Operations ===

async function handleRun() {
  if (!currentSequence?.value?.id) {
    ElMessage.warning('No sequence selected. Please select or create a sequence first.')
    return
  }

  // Save the sequence first before running
  if (graphInstance?.value) {
    try {
      const yamlContent = graphToYaml(graphInstance.value, {
        name: currentSequence.value.name,
        version: currentSequence.value.version,
      })
      await updateSequence(currentSequence.value.id, {
        yaml_content: yamlContent,
      })
    } catch (error) {
      console.error('Error saving sequence before run:', error)
      ElMessage.error('Failed to save sequence before running')
      return
    }
  }

  isStartingExecution.value = true
  try {
    const execution = await createExecution({
      sequence_id: currentSequence.value.id,
    })
    currentRunId.value = execution.id
    emit('executionStarted', execution.id)
    ElMessage.success('Execution started')
  } catch (error) {
    console.error('Error starting execution:', error)
    ElMessage.error('Failed to start execution')
  } finally {
    isStartingExecution.value = false
  }
}

async function handleAbort() {
  if (!currentRunId.value) return

  isAbortingExecution.value = true
  try {
    await abortExecution(currentRunId.value)
    ElMessage.info('Execution abort requested')
  } catch (error) {
    console.error('Error aborting execution:', error)
    ElMessage.error('Failed to abort execution')
  } finally {
    isAbortingExecution.value = false
  }
}

// Watch for execution completion to emit event
const wasRunning = ref(false)
watch(isRunning, (running, prev) => {
  if (prev && !running && currentRunId.value) {
    // Execution just ended
    emit('executionEnded')
    // Don't clear runId immediately — let user see final status
  }
})

// Status indicator computed
const statusIndicator = computed(() => {
  if (isRunning.value) return { color: '#3b82f6', text: progressText.value || 'Running...' }
  if (executionStatus.value === 'COMPLETED') return { color: '#22c55e', text: progressText.value || 'Completed' }
  if (executionStatus.value === 'FAILED') return { color: '#ef4444', text: progressText.value || 'Failed' }
  if (executionStatus.value === 'ABORTED') return { color: '#f97316', text: 'Aborted' }
  return { color: '#22c55e', text: 'Ready' }
})

// === File Operations ===

async function handleSave() {
  if (!graphInstance?.value) {
    ElMessage.warning('No graph to save')
    return
  }
  
  if (!currentSequence?.value?.id) {
    ElMessage.warning('No sequence selected. Please select or create a sequence first.')
    return
  }
  
  try {
    // Convert graph to YAML
    const yamlContent = graphToYaml(graphInstance.value, {
      name: currentSequence.value.name,
      version: currentSequence.value.version,
    })
    
    // Save to backend
    await updateSequence(currentSequence.value.id, {
      yaml_content: yamlContent,
    })
    
    ElMessage.success('Sequence saved successfully')
  } catch (error) {
    console.error('Error saving sequence:', error)
    ElMessage.error('Failed to save sequence')
  }
}

function handleExportYaml() {
  if (!graphInstance?.value) {
    ElMessage.warning('No graph to export')
    return
  }
  
  try {
    exportGraphToYaml(graphInstance.value, 'sequence.yaml')
    ElMessage.success('YAML exported successfully')
  } catch (error) {
    ElMessage.error('Failed to export YAML')
    console.error('Export error:', error)
  }
}

function handleImportYaml() {
  if (!graphInstance?.value) {
    ElMessage.warning('No graph available')
    return
  }
  
  // Create file input
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = '.yaml,.yml'
  
  input.onchange = async (event) => {
    const file = (event.target as HTMLInputElement).files?.[0]
    if (!file) return
    
    try {
      const content = await file.text()
      importYamlToGraph(graphInstance.value!, content, true, { autoLayout: autoLayoutEnabled.value })
      ElMessage.success('YAML imported successfully')
      updateZoomFromGraph()
    } catch (error) {
      ElMessage.error('Failed to import YAML: Invalid format')
      console.error('Import error:', error)
    }
  }
  
  input.click()
}

// === History Operations ===

function handleUndo() {
  if (!graphInstance?.value) return
  
  const history = graphInstance.value.getPlugin('history') as History | undefined
  if (history && history.canUndo()) {
    history.undo()
  }
}

function handleRedo() {
  if (!graphInstance?.value) return
  
  const history = graphInstance.value.getPlugin('history') as History | undefined
  if (history && history.canRedo()) {
    history.redo()
  }
}

// === Zoom Operations ===

function updateZoomFromGraph() {
  if (!graphInstance?.value) return
  
  const zoom = graphInstance.value.zoom()
  zoomLevel.value = Math.round(zoom * 100)
}

function handleZoomIn() {
  if (!graphInstance?.value) return
  
  const newZoom = Math.min(zoomLevel.value + 10, 200)
  graphInstance.value.zoom(newZoom / 100)
  zoomLevel.value = newZoom
}

function handleZoomOut() {
  if (!graphInstance?.value) return
  
  const newZoom = Math.max(zoomLevel.value - 10, 25)
  graphInstance.value.zoom(newZoom / 100)
  zoomLevel.value = newZoom
}

function handleZoomReset() {
  if (!graphInstance?.value) return
  
  graphInstance.value.zoom(1)
  zoomLevel.value = 100
}

function handleFitContent() {
  if (!graphInstance?.value) return
  
  graphInstance.value.zoomToFit({ padding: 40, maxScale: 1.5 })
  updateZoomFromGraph()
}

// === Layout Operations ===

function handleAlignLeft() {
  if (!graphInstance?.value) return
  
  const nodes = graphInstance.value.getSelectedNodes()
  if (nodes.length < 2) {
    ElMessage.info('Select at least 2 nodes to align')
    return
  }
  
  const minX = Math.min(...nodes.map(n => n.getPosition().x))
  nodes.forEach(node => {
    node.setPosition(minX, node.getPosition().y)
  })
}

function handleAlignCenter() {
  if (!graphInstance?.value) return
  
  const nodes = graphInstance.value.getSelectedNodes()
  if (nodes.length < 2) {
    ElMessage.info('Select at least 2 nodes to align')
    return
  }
  
  const avgX = nodes.reduce((sum, n) => sum + n.getPosition().x, 0) / nodes.length
  nodes.forEach(node => {
    node.setPosition(avgX, node.getPosition().y)
  })
}

function handleAlignTop() {
  if (!graphInstance?.value) return
  
  const nodes = graphInstance.value.getSelectedNodes()
  if (nodes.length < 2) {
    ElMessage.info('Select at least 2 nodes to align')
    return
  }
  
  const minY = Math.min(...nodes.map(n => n.getPosition().y))
  nodes.forEach(node => {
    node.setPosition(node.getPosition().x, minY)
  })
}

// === Keyboard Shortcuts ===

function handleKeydown(event: KeyboardEvent) {
  const isMac = navigator.platform.toUpperCase().indexOf('MAC') >= 0
  const cmdKey = isMac ? event.metaKey : event.ctrlKey
  
  if (!cmdKey) return
  
  switch (event.key.toLowerCase()) {
    case 's':
      event.preventDefault()
      handleSave()
      break
    case 'z':
      event.preventDefault()
      if (event.shiftKey) {
        handleRedo()
      } else {
        handleUndo()
      }
      break
    case 'y':
      event.preventDefault()
      handleRedo()
      break
  }
}

onMounted(() => {
  document.addEventListener('keydown', handleKeydown)
})

onUnmounted(() => {
  document.removeEventListener('keydown', handleKeydown)
})
</script>

<template>
  <div class="tw-flex tw-items-center tw-gap-4 tw-w-full">
    <!-- Sequence selector -->
    <div class="tw-flex tw-items-center tw-gap-2">
      <ElSelect
        v-model="selectedSequenceId"
        placeholder="Select sequence..."
        class="sequence-selector"
        :loading="isLoadingSequences"
        :disabled="isLoadingSequences"
        @change="handleSequenceChange"
      >
        <ElOption
          v-for="seq in sequences"
          :key="seq.id"
          :label="seq.name"
          :value="seq.id"
        />
      </ElSelect>
      
      <button
        class="tw-px-3 tw-py-1.5 tw-text-sm tw-font-medium tw-text-white tw-bg-success tw-rounded-md hover:tw-opacity-90 tw-transition-opacity tw-flex tw-items-center tw-gap-1.5"
        title="Create new sequence"
        :disabled="isLoadingSequences"
        @click="handleNewSequence"
      >
        <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
        </svg>
        New
      </button>
    </div>

    <!-- Divider -->
    <div class="tw-w-px tw-h-6 tw-bg-neutral-200"></div>

    <!-- File operations -->
    <div class="tw-flex tw-items-center tw-gap-2">
      <button
        class="tw-px-3 tw-py-1.5 tw-text-sm tw-font-medium tw-text-white tw-bg-primary-600 tw-rounded-md hover:tw-bg-primary-700 tw-transition-colors tw-flex tw-items-center tw-gap-1.5"
        title="Save (Ctrl+S)"
        @click="handleSave"
      >
        <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7H5a2 2 0 00-2 2v9a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-3m-1 4l-3 3m0 0l-3-3m3 3V4" />
        </svg>
        Save
      </button>
      
      <button
        class="tw-px-3 tw-py-1.5 tw-text-sm tw-font-medium tw-text-neutral-700 tw-bg-neutral-100 tw-rounded-md hover:tw-bg-neutral-200 tw-transition-colors tw-flex tw-items-center tw-gap-1.5"
        title="Export YAML"
        @click="handleExportYaml"
      >
        <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
        </svg>
        Export
      </button>
      
      <button
        class="tw-px-3 tw-py-1.5 tw-text-sm tw-font-medium tw-text-neutral-700 tw-bg-neutral-100 tw-rounded-md hover:tw-bg-neutral-200 tw-transition-colors tw-flex tw-items-center tw-gap-1.5"
        title="Import YAML"
        @click="handleImportYaml"
      >
        <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
        </svg>
        Import
      </button>
    </div>

    <!-- Divider -->
    <div class="tw-w-px tw-h-6 tw-bg-neutral-200"></div>

    <!-- Undo/Redo -->
    <div class="tw-flex tw-items-center tw-gap-1">
      <button
        class="tw-p-1.5 tw-text-neutral-600 hover:tw-text-neutral-800 hover:tw-bg-neutral-100 tw-rounded tw-transition-colors disabled:tw-opacity-40 disabled:tw-cursor-not-allowed"
        :class="{ 'tw-opacity-40': !canUndo }"
        :disabled="!canUndo"
        title="Undo (Ctrl+Z)"
        @click="handleUndo"
      >
        <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 10h10a8 8 0 018 8v2M3 10l6 6m-6-6l6-6" />
        </svg>
      </button>
      
      <button
        class="tw-p-1.5 tw-text-neutral-600 hover:tw-text-neutral-800 hover:tw-bg-neutral-100 tw-rounded tw-transition-colors disabled:tw-opacity-40 disabled:tw-cursor-not-allowed"
        :class="{ 'tw-opacity-40': !canRedo }"
        :disabled="!canRedo"
        title="Redo (Ctrl+Y / Ctrl+Shift+Z)"
        @click="handleRedo"
      >
        <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 10h-10a8 8 0 00-8 8v2M21 10l-6 6m6-6l-6-6" />
        </svg>
      </button>
    </div>

    <!-- Divider -->
    <div class="tw-w-px tw-h-6 tw-bg-neutral-200"></div>

    <!-- Zoom controls -->
    <div class="tw-flex tw-items-center tw-gap-1">
      <button
        class="tw-p-1.5 tw-text-neutral-600 hover:tw-text-neutral-800 hover:tw-bg-neutral-100 tw-rounded tw-transition-colors"
        title="Zoom Out"
        @click="handleZoomOut"
      >
        <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 12H4" />
        </svg>
      </button>
      
      <span class="tw-w-12 tw-text-center tw-text-sm tw-font-medium tw-text-neutral-700 tw-select-none">
        {{ zoomDisplay }}
      </span>
      
      <button
        class="tw-p-1.5 tw-text-neutral-600 hover:tw-text-neutral-800 hover:tw-bg-neutral-100 tw-rounded tw-transition-colors"
        title="Zoom In"
        @click="handleZoomIn"
      >
        <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
        </svg>
      </button>
      
      <button
        class="tw-p-1.5 tw-text-neutral-600 hover:tw-text-neutral-800 hover:tw-bg-neutral-100 tw-rounded tw-transition-colors"
        title="Reset Zoom (100%)"
        @click="handleZoomReset"
      >
        <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 8V4m0 0h4M4 4l5 5m11-1V4m0 0h-4m4 0l-5 5M4 16v4m0 0h4m-4 0l5-5m11 5l-5-5m5 5v-4m0 4h-4" />
        </svg>
      </button>
      
      <button
        class="tw-p-1.5 tw-text-neutral-600 hover:tw-text-neutral-800 hover:tw-bg-neutral-100 tw-rounded tw-transition-colors"
        title="Fit to Screen"
        @click="handleFitContent"
      >
        <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 8V4m0 0h4M4 4l5 5m11-1V4m0 0h-4m4 0l-5 5M4 16v4m0 0h4m-4 0l5-5m11 5l-5-5m5 5v-4m0 4h-4" />
        </svg>
      </button>
    </div>

    <!-- Divider -->
    <div class="tw-w-px tw-h-6 tw-bg-neutral-200"></div>

    <!-- Layout alignment -->
    <div class="tw-flex tw-items-center tw-gap-1">
      <button
        class="tw-p-1.5 tw-text-neutral-600 hover:tw-text-neutral-800 hover:tw-bg-neutral-100 tw-rounded tw-transition-colors"
        title="Align Left"
        @click="handleAlignLeft"
      >
        <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h10M4 18h14" />
        </svg>
      </button>
      
      <button
        class="tw-p-1.5 tw-text-neutral-600 hover:tw-text-neutral-800 hover:tw-bg-neutral-100 tw-rounded tw-transition-colors"
        title="Align Center"
        @click="handleAlignCenter"
      >
        <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M7 12h10M5 18h14" />
        </svg>
      </button>
      
      <button
        class="tw-p-1.5 tw-text-neutral-600 hover:tw-text-neutral-800 hover:tw-bg-neutral-100 tw-rounded tw-transition-colors"
        title="Align Top"
        @click="handleAlignTop"
      >
        <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M10 12h4M8 18h8" />
        </svg>
      </button>
    </div>

    <!-- Divider -->
    <div class="tw-w-px tw-h-6 tw-bg-neutral-200"></div>

    <!-- Auto-layout toggle -->
    <div class="tw-flex tw-items-center tw-gap-1">
      <button
        class="tw-p-1.5 tw-rounded tw-transition-colors tw-flex tw-items-center tw-gap-1 tw-text-sm tw-font-medium"
        :class="autoLayoutEnabled ? 'tw-text-primary-600 tw-bg-primary-50 hover:tw-bg-primary-100' : 'tw-text-neutral-400 tw-bg-neutral-50 hover:tw-bg-neutral-100'"
        title="Toggle auto-layout (dagre)"
        @click="autoLayoutEnabled = !autoLayoutEnabled"
      >
        <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 5a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1H5a1 1 0 01-1-1V5zm10 0a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1V5zM4 15a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1H5a1 1 0 01-1-1v-4zm10 0a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1v-4z" />
        </svg>
        <span v-if="autoLayoutEnabled" class="tw-text-xs">Auto</span>
        <span v-else class="tw-text-xs">Manual</span>
      </button>
    </div>

    <!-- Divider -->
    <div class="tw-w-px tw-h-6 tw-bg-neutral-200"></div>

    <!-- Spacer -->
    <div class="tw-flex-1"></div>

    <!-- Execution controls -->
    <div class="tw-flex tw-items-center tw-gap-2">
      <!-- Run button -->
      <button
        class="tw-px-3 tw-py-1.5 tw-text-sm tw-font-medium tw-text-white tw-bg-green-600 tw-rounded-md hover:tw-bg-green-700 tw-transition-colors tw-flex tw-items-center tw-gap-1.5 disabled:tw-opacity-50 disabled:tw-cursor-not-allowed"
        :disabled="isRunning || isStartingExecution || !currentSequence?.value?.id"
        @click="handleRun"
      >
        <svg v-if="!isStartingExecution" class="tw-w-4 tw-h-4" fill="currentColor" viewBox="0 0 24 24">
          <path d="M8 5v14l11-7z" />
        </svg>
        <svg v-else class="tw-w-4 tw-h-4 tw-animate-spin" fill="none" viewBox="0 0 24 24">
          <circle class="tw-opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
          <path class="tw-opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
        </svg>
        Run
      </button>

      <!-- Abort button (only visible when execution is running) -->
      <button
        v-if="isRunning"
        class="tw-px-3 tw-py-1.5 tw-text-sm tw-font-medium tw-text-white tw-bg-red-600 tw-rounded-md hover:tw-bg-red-700 tw-transition-colors tw-flex tw-items-center tw-gap-1.5 disabled:tw-opacity-50 disabled:tw-cursor-not-allowed"
        :disabled="isAbortingExecution"
        @click="handleAbort"
      >
        <svg class="tw-w-4 tw-h-4" fill="currentColor" viewBox="0 0 24 24">
          <rect x="6" y="6" width="12" height="12" rx="1" />
        </svg>
        Abort
      </button>
    </div>

    <!-- Divider -->
    <div class="tw-w-px tw-h-6 tw-bg-neutral-200"></div>

    <!-- Status indicator -->
    <div class="tw-flex tw-items-center tw-gap-2 tw-text-sm tw-text-neutral-500">
      <span
        class="tw-w-2 tw-h-2 tw-rounded-full"
        :class="{ 'tw-animate-pulse': isRunning }"
        :style="{ backgroundColor: statusIndicator.color }"
      ></span>
      <span>{{ statusIndicator.text }}</span>
    </div>
  </div>
</template>

<style scoped>
/* Sequence selector styling */
.sequence-selector {
  width: 200px;
}

/* Ensure Element Plus select matches our design system */
:deep(.el-input__wrapper) {
  border-radius: 6px;
  box-shadow: 0 0 0 1px #d1d5db;
  transition: box-shadow 0.15s ease;
}

:deep(.el-input__wrapper:hover) {
  box-shadow: 0 0 0 1px #9ca3af;
}

:deep(.el-input__wrapper.is-focus) {
  box-shadow: 0 0 0 2px #3b82f6;
}

:deep(.el-input__inner) {
  font-size: 14px;
  color: #374151;
}
</style>
