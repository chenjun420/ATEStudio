<script setup lang="ts">
import { inject, computed, watch, ref, provide } from 'vue'
import type { Ref, ShallowRef } from 'vue'
import type { Graph, Node } from '@antv/x6'
import { isScriptStepData, isVariableData, isLoopContainerData, type ScriptStepData, type VariableData, type LoopContainerData } from '@/models/nodes/types'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import NodeTemplateDialog from './NodeTemplateDialog.vue'
import { useBatchEdit, type BatchEditProperties } from '@/composables/useBatchEdit'
import MonacoEditor from '@/components/MonacoEditor.vue'
import { searchScripts, fetchScriptContent } from '@/api/scripts'

// Emit events to parent
const emit = defineEmits<{
  /** Emitted when user wants to open the full script editor dialog */
  'edit-script': [payload: { scriptId: string; scriptName: string }]
}>()

// Get selected node ID and graph instance from parent
const selectedNodeId = inject<Ref<string | null>>('selectedNodeId')
const graphInstance = inject<ShallowRef<Graph | null>>('graphInstance')

// Batch edit mode state (injected from parent or managed locally)
const batchNodeIds = inject<Ref<string[]>>('batchNodeIds', ref([]))

// Template dialog state
const showTemplateDialog = ref(false)

// Batch edit composable
const {
  isBatchMode,
  hasSelection,
  selectionCount,
  getCommonProperties,
  applyBatchEdits,
  batchDelete,
} = useBatchEdit(graphInstance)

// Batch edit form data
const batchEditData = ref<BatchEditProperties>({
  timeout: undefined,
  onFail: undefined,
  exportOutputs: undefined,
})

// Watch batch node IDs to update selection
watch(batchNodeIds, (ids) => {
  // Update batch edit form with common properties
  if (ids.length > 1) {
    const commonProps = getCommonProperties()
    batchEditData.value = {
      timeout: commonProps.timeout,
      onFail: commonProps.onFail,
      exportOutputs: commonProps.exportOutputs,
    }
  }
}, { immediate: true })

// Local reactive data for editing
const localScriptData = ref<ScriptStepData | null>(null)
const localVariableData = ref<VariableData | null>(null)
const localLoopData = ref<LoopContainerData | null>(null)

// New parameter key for adding
const newParamKey = ref('')
const newParamValue = ref('')

// New resource for adding
const newResource = ref('')

// ============================================
// Script Preview State
// ============================================

// Script content preview for Monaco inline display
const scriptPreviewContent = ref('')
const scriptPreviewLoading = ref(false)
const scriptPreviewError = ref<string | null>(null)
const resolvedScriptId = ref<string | null>(null)

// First 10 lines of script content for preview
const scriptPreviewLines = computed(() => {
  if (!scriptPreviewContent.value) return ''
  const lines = scriptPreviewContent.value.split('\n')
  return lines.slice(0, 10).join('\n')
})

// Failure strategy options
const failOptions = [
  { value: 'stop', label: 'Stop sequence' },
  { value: 'skip', label: 'Skip and continue' },
  { value: 'ignore', label: 'Ignore and continue' },
]

// Computed property for selected node
const selectedNode = computed<Node | null>(() => {
  if (!selectedNodeId?.value || !graphInstance?.value) return null
  return graphInstance.value.getCellById(selectedNodeId.value) as Node | null
})

// Computed property for node type
const nodeType = computed<string>(() => {
  if (!selectedNode.value) return 'unknown'
  return selectedNode.value.shape || 'unknown'
})

// Computed property for node data
const nodeData = computed<unknown>(() => {
  return selectedNode.value?.getData()
})

// Determine node category via type-switch supporting all 3 node types
const nodeCategory = computed<'script-step' | 'variable' | 'loop-container' | 'unknown'>(() => {
  if (nodeType.value === 'script-step-node' || isScriptStepData(nodeData.value)) return 'script-step'
  if (nodeType.value === 'variable-node' || isVariableData(nodeData.value)) return 'variable'
  if (nodeType.value === 'loop-container-node' || (nodeData.value && isLoopContainerData(nodeData.value as any))) return 'loop-container'
  return 'unknown'
})

// Convenience computed flags (derived from nodeCategory)
const isScriptStep = computed(() => nodeCategory.value === 'script-step')
const isVariable = computed(() => nodeCategory.value === 'variable')
const isLoopContainer = computed(() => nodeCategory.value === 'loop-container')

// Status badge styling
const statusStyles: Record<string, string> = {
  idle: 'tw-bg-neutral-100 tw-text-neutral-600',
  running: 'tw-bg-blue-100 tw-text-blue-600',
  passed: 'tw-bg-green-100 tw-text-green-600',
  failed: 'tw-bg-red-100 tw-text-red-600',
  error: 'tw-bg-orange-100 tw-text-orange-600',
}

// ============================================
// Appearance Customization
// ============================================

// Preset colors for node appearance (using design tokens + common colors)
const presetColors = [
  { name: 'Default', value: '#e5e7eb', cssVar: 'var(--color-border-default)' },
  { name: 'Primary', value: '#409eff', cssVar: 'var(--color-primary)' },
  { name: 'Blue', value: '#3b82f6', cssVar: 'var(--color-info)' },
  { name: 'Green', value: '#10b981', cssVar: 'var(--color-success)' },
  { name: 'Orange', value: '#f59e0b', cssVar: 'var(--color-warning)' },
  { name: 'Red', value: '#ef4444', cssVar: 'var(--color-error)' },
  { name: 'Cyan', value: '#06b6d4' },
  { name: 'Pink', value: '#ec4899' },
  { name: 'Indigo', value: '#6366f1' },
  { name: 'Teal', value: '#14b8a6' },
]

// Popular Element Plus icons for node icons
const nodeIconOptions = [
  { name: 'None', icon: null },
  { name: 'Gear', icon: 'Setting' },
  { name: 'Document', icon: 'Document' },
  { name: 'Lightning', icon: 'Lightning' },
  { name: 'Check', icon: 'Check' },
  { name: 'Close', icon: 'Close' },
  { name: 'Warning', icon: 'Warning' },
  { name: 'Info', icon: 'InfoFilled' },
  { name: 'Question', icon: 'QuestionFilled' },
  { name: 'Star', icon: 'Star' },
  { name: 'Timer', icon: 'Timer' },
  { name: 'Clock', icon: 'Clock' },
  { name: 'Data', icon: 'DataAnalysis' },
  { name: 'Tools', icon: 'Tools' },
  { name: 'Connection', icon: 'Connection' },
]

// Current appearance state
const currentBorderColor = ref<string>('#e5e7eb')
const currentBackgroundColor = ref<string>('#ffffff')
const currentIcon = ref<string | null>(null)

// Get icon component by name
function getIconComponent(iconName: string | null) {
  if (!iconName) return null
  return (ElementPlusIconsVue as Record<string, unknown>)[iconName] || null
}

// Update node border color
function updateBorderColor(color: string) {
  if (!selectedNode.value) return
  
  currentBorderColor.value = color
  
  // Update node attrs using X6 API
  selectedNode.value.setAttrs({
    body: {
      stroke: color,
    },
  })
}

// Update node background color
function updateBackgroundColor(color: string) {
  if (!selectedNode.value) return
  
  currentBackgroundColor.value = color
  
  // Update node attrs using X6 API
  selectedNode.value.setAttrs({
    body: {
      fill: color,
    },
  })
}

// Update node icon (stored in node data)
function updateNodeIcon(iconName: string | null) {
  if (!selectedNode.value) return
  
  currentIcon.value = iconName
  
  // Store icon in node data for potential rendering
  const data = selectedNode.value.getData() as Record<string, unknown>
  selectedNode.value.setData({
    ...data,
    nodeIcon: iconName,
  })
}

// Watch for node selection to sync appearance state
watch(selectedNode, (node) => {
  if (!node) {
    currentBorderColor.value = '#e5e7eb'
    currentBackgroundColor.value = '#ffffff'
    currentIcon.value = null
    return
  }
  
  // Get current attrs from node
  const attrs = node.getAttrs()
  if (attrs?.body) {
    currentBorderColor.value = (attrs.body as { stroke?: string }).stroke || '#e5e7eb'
    currentBackgroundColor.value = (attrs.body as { fill?: string }).fill || '#ffffff'
  }
  
  // Get icon from node data
  const data = node.getData() as Record<string, unknown> | undefined
  currentIcon.value = (data?.nodeIcon as string) || null
}, { immediate: true })

// Watch for node selection changes and sync local data
watch(selectedNodeId, (newId) => {
  if (!newId || !selectedNode.value) {
    localScriptData.value = null
    localVariableData.value = null
    localLoopData.value = null
    // Clear script preview
    scriptPreviewContent.value = ''
    scriptPreviewError.value = null
    resolvedScriptId.value = null
    return
  }

  const data = selectedNode.value.getData()
  if (isScriptStepData(data)) {
    localScriptData.value = { ...data }
    localVariableData.value = null
    localLoopData.value = null
    // Load script preview
    loadScriptPreview(data.scriptName)
  } else if (isVariableData(data)) {
    localVariableData.value = { ...data, variables: { ...data.variables } }
    localScriptData.value = null
    localLoopData.value = null
    // Clear script preview for non-script nodes
    scriptPreviewContent.value = ''
    scriptPreviewError.value = null
    resolvedScriptId.value = null
  } else if (isLoopContainerData(data as any)) {
    localLoopData.value = { ...data }
    localScriptData.value = null
    localVariableData.value = null
    // Clear script preview for non-script nodes
    scriptPreviewContent.value = ''
    scriptPreviewError.value = null
    resolvedScriptId.value = null
  }
}, { immediate: true })

// Watch node data for external changes
watch(nodeData, (newData) => {
  if (isScriptStepData(newData) && localScriptData.value) {
    // Only update if values differ to avoid losing user input
    if (JSON.stringify(newData) !== JSON.stringify(localScriptData.value)) {
      localScriptData.value = { ...newData }
    }
  } else if (isVariableData(newData) && localVariableData.value) {
    if (JSON.stringify(newData) !== JSON.stringify(localVariableData.value)) {
      localVariableData.value = { ...newData, variables: { ...newData.variables } }
    }
  } else if (newData && isLoopContainerData(newData as any) && localLoopData.value) {
    if (JSON.stringify(newData) !== JSON.stringify(localLoopData.value)) {
      localLoopData.value = { ...newData }
    }
  }
}, { deep: true })

// Update node data in graph
function updateNodeData() {
  if (!selectedNode.value) return

  if (isScriptStep.value && localScriptData.value) {
    selectedNode.value.setData(localScriptData.value, { silent: false })
  } else if (isVariable.value && localVariableData.value) {
    selectedNode.value.setData(localVariableData.value, { silent: false })
  } else if (isLoopContainer.value && localLoopData.value) {
    selectedNode.value.setData(localLoopData.value, { silent: false })
  }
}

// Add a new parameter
function addParameter() {
  if (!newParamKey.value.trim()) return
  if (!localScriptData.value) return

  if (!localScriptData.value.params) {
    localScriptData.value.params = {}
  }
  localScriptData.value.params[newParamKey.value.trim()] = newParamValue.value || ''
  newParamKey.value = ''
  newParamValue.value = ''
  updateNodeData()
}

// Remove a parameter
function removeParameter(key: string) {
  if (!localScriptData.value?.params) return
  delete localScriptData.value.params[key]
  updateNodeData()
}

// Add a new resource
function addResource() {
  if (!newResource.value.trim()) return
  if (!localScriptData.value) return

  if (!localScriptData.value.resources) {
    localScriptData.value.resources = []
  }
  localScriptData.value.resources.push(newResource.value.trim())
  newResource.value = ''
  updateNodeData()
}

// Remove a resource
function removeResource(index: number) {
  if (!localScriptData.value?.resources) return
  localScriptData.value.resources.splice(index, 1)
  updateNodeData()
}

// Add a variable
function addVariable() {
  if (!newParamKey.value.trim()) return
  if (!localVariableData.value) return

  localVariableData.value.variables[newParamKey.value.trim()] = newParamValue.value || ''
  newParamKey.value = ''
  newParamValue.value = ''
  updateNodeData()
}

// Remove a variable
function removeVariable(key: string) {
  if (!localVariableData.value?.variables) return
  delete localVariableData.value.variables[key]
  updateNodeData()
}

// Loop container update helpers
function updateLoopType(loopType: 'for' | 'while' | 'foreach') {
  if (!localLoopData.value) return
  localLoopData.value.loopType = loopType
  // Reset fields not relevant to the new loop type
  if (loopType === 'for') {
    localLoopData.value.condition = ''
    localLoopData.value.iterationVar = undefined
    localLoopData.value.collectionExpr = undefined
    if (localLoopData.value.count === undefined) localLoopData.value.count = 3
  } else if (loopType === 'while') {
    localLoopData.value.count = undefined
    localLoopData.value.iterationVar = undefined
    localLoopData.value.collectionExpr = undefined
  } else if (loopType === 'foreach') {
    localLoopData.value.count = undefined
    if (!localLoopData.value.iterationVar) localLoopData.value.iterationVar = 'item'
    if (!localLoopData.value.collectionExpr) localLoopData.value.collectionExpr = ''
  }
  updateNodeData()
}

function updateExecutionMode(mode: 'serial' | 'parallel') {
  if (!localLoopData.value) return
  localLoopData.value.executionMode = mode
  if (mode === 'serial') {
    localLoopData.value.maxConcurrency = 1
  } else if (localLoopData.value.maxConcurrency <= 1) {
    localLoopData.value.maxConcurrency = 4
  }
  updateNodeData()
}

// Open template dialog
function openTemplateDialog() {
  showTemplateDialog.value = true
}

// Handle template saved
function handleTemplateSaved() {
  // Template successfully saved - could show notification
}

// Batch edit functions
function applyBatchChanges() {
  const result = applyBatchEdits(batchEditData.value)
  
  if (result.success) {
    console.log(`Batch edit applied to ${result.updatedCount} nodes`)
    // Could show success notification
  } else {
    console.error('Batch edit errors:', result.errors)
    // Could show error notification
  }
}

function handleBatchDelete() {
  const result = batchDelete()
  
  if (result.success) {
    console.log(`Deleted ${result.deletedCount} nodes`)
    // Clear batch selection
    batchNodeIds.value = []
  } else {
    console.error('Batch delete errors:', result.errors)
  }
}

function clearBatchSelection() {
  batchNodeIds.value = []
}

// ============================================
// Script Preview Functions
// ============================================

/**
 * Load script content preview when a script step is selected.
 * Resolves scriptId from scriptName via search API, then fetches content.
 */
async function loadScriptPreview(scriptName: string) {
  if (!scriptName) {
    scriptPreviewContent.value = ''
    scriptPreviewError.value = null
    resolvedScriptId.value = null
    return
  }

  scriptPreviewLoading.value = true
  scriptPreviewError.value = null

  try {
    // Resolve script ID from script name
    const scripts = await searchScripts(scriptName)
    const matchedScript = scripts.find(s => s.name === scriptName)

    if (!matchedScript) {
      scriptPreviewError.value = `Script "${scriptName}" not found`
      scriptPreviewContent.value = ''
      resolvedScriptId.value = null
      return
    }

    resolvedScriptId.value = matchedScript.id

    // Fetch script content
    const response = await fetchScriptContent(matchedScript.id)
    scriptPreviewContent.value = response.content
  } catch (err) {
    scriptPreviewError.value = err instanceof Error ? err.message : 'Failed to load script content'
    scriptPreviewContent.value = ''
  } finally {
    scriptPreviewLoading.value = false
  }
}

/**
 * Open the full script editor dialog
 */
function openScriptEditor() {
  if (!resolvedScriptId.value || !localScriptData.value) return
  emit('edit-script', {
    scriptId: resolvedScriptId.value,
    scriptName: localScriptData.value.scriptName,
  })
}
</script>

<template>
  <div class="tw-h-full tw-flex tw-flex-col tw-bg-white">
    <!-- Panel header -->
    <div class="tw-px-4 tw-py-3 tw-border-b tw-border-neutral-200">
      <h2 v-if="batchNodeIds.length > 1" class="tw-text-sm tw-font-semibold tw-text-neutral-800">
        Batch Edit ({{ batchNodeIds.length }} nodes)
      </h2>
      <h2 v-else class="tw-text-sm tw-font-semibold tw-text-neutral-800">Properties</h2>
    </div>

    <!-- Property content -->
    <div class="tw-flex-1 tw-overflow-y-auto">
      <!-- Batch Edit Mode -->
      <div v-if="batchNodeIds.length > 1" class="tw-p-4 tw-space-y-5">
        <!-- Batch Info -->
        <section class="tw-bg-purple-50 tw-rounded-lg tw-p-4">
          <div class="tw-flex tw-items-center tw-gap-2 tw-mb-2">
            <svg class="tw-w-5 tw-h-5 tw-text-purple-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" />
            </svg>
            <span class="tw-text-sm tw-font-semibold tw-text-purple-900">{{ batchNodeIds.length }} nodes selected</span>
          </div>
          <p class="tw-text-xs tw-text-purple-700">
            Edit common properties for all selected nodes at once.
          </p>
        </section>

        <!-- Batch Edit Properties -->
        <section class="tw-space-y-4">
          <h3 class="tw-text-xs tw-font-semibold tw-text-neutral-500 tw-uppercase tw-tracking-wide">
            Common Properties
          </h3>

          <!-- Timeout -->
          <div>
            <label class="tw-block tw-text-sm tw-font-medium tw-text-neutral-700 tw-mb-1">
              Timeout (ms)
            </label>
            <el-input-number
              v-model="batchEditData.timeout"
              :min="0"
              :step="1000"
              size="small"
              class="tw-w-full"
              placeholder="Leave unchanged"
            />
          </div>

          <!-- Failure Strategy -->
          <div>
            <label class="tw-block tw-text-sm tw-font-medium tw-text-neutral-700 tw-mb-1">
              On Failure
            </label>
            <el-select
              v-model="batchEditData.onFail"
              size="small"
              class="tw-w-full"
              clearable
              placeholder="Leave unchanged"
            >
              <el-option
                v-for="option in failOptions"
                :key="option.value"
                :label="option.label"
                :value="option.value"
              />
            </el-select>
          </div>

          <!-- Export Outputs -->
          <div class="tw-flex tw-items-center tw-gap-2">
            <el-checkbox v-model="batchEditData.exportOutputs" />
            <label class="tw-text-sm tw-text-neutral-700">Export outputs</label>
          </div>
        </section>

        <!-- Batch Actions -->
        <section class="tw-space-y-3 tw-pt-4 tw-border-t tw-border-neutral-200">
          <button
            class="tw-w-full tw-px-4 tw-py-2.5 tw-text-sm tw-font-semibold tw-text-white tw-bg-primary-600 tw-rounded-lg hover:tw-bg-primary-700 tw-transition-colors tw-flex tw-items-center tw-justify-center tw-gap-2"
            @click="applyBatchChanges"
          >
            <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
            </svg>
            Apply Changes
          </button>

          <div class="tw-flex tw-gap-2">
            <button
              class="tw-flex-1 tw-px-3 tw-py-2 tw-text-sm tw-font-medium tw-text-neutral-700 tw-bg-neutral-100 tw-rounded-md hover:tw-bg-neutral-200 tw-transition-colors"
              @click="clearBatchSelection"
            >
              Cancel
            </button>
            <button
              class="tw-flex-1 tw-px-3 tw-py-2 tw-text-sm tw-font-medium tw-text-red-600 tw-bg-red-50 tw-rounded-md hover:tw-bg-red-100 tw-transition-colors tw-flex tw-items-center tw-justify-center tw-gap-1"
              @click="handleBatchDelete"
            >
              <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
              </svg>
              Delete All
            </button>
          </div>
        </section>
      </div>

      <!-- Single Node Edit Mode -->
      <template v-else>
        <!-- No selection state -->
        <div v-if="!selectedNode" class="tw-flex tw-flex-col tw-items-center tw-justify-center tw-h-full tw-p-8">
          <div class="tw-w-16 tw-h-16 tw-rounded-full tw-bg-neutral-100 tw-flex tw-items-center tw-justify-center tw-mb-4">
            <svg class="tw-w-8 tw-h-8 tw-text-neutral-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
            </svg>
          </div>
          <p class="tw-text-sm tw-text-neutral-500 tw-text-center">
            Select a node to view<br />and edit its properties
          </p>
        </div>

      <!-- Selected node properties -->
      <div v-else class="tw-p-4 tw-space-y-5">
         <!-- Node Info Section -->
        <section class="tw-bg-neutral-50 tw-rounded-lg tw-p-4">
          <h3 class="tw-text-xs tw-font-semibold tw-text-neutral-500 tw-uppercase tw-tracking-wide tw-mb-3">
            Node Info
          </h3>
          <div class="tw-space-y-2 tw-text-sm">
            <div class="tw-flex tw-justify-between">
              <span class="tw-text-neutral-500">ID</span>
              <span class="tw-font-mono tw-text-neutral-800 tw-text-xs">{{ selectedNodeId }}</span>
            </div>
            <div class="tw-flex tw-justify-between">
              <span class="tw-text-neutral-500">Type</span>
              <span class="tw-text-neutral-800 tw-capitalize">{{ nodeType.replace(/-/g, ' ') }}</span>
            </div>
            <div v-if="localScriptData?.status || localLoopData?.status" class="tw-flex tw-justify-between tw-items-center">
              <span class="tw-text-neutral-500">Status</span>
              <span 
                class="tw-px-2 tw-py-0.5 tw-rounded-full tw-text-xs tw-font-medium"
                :class="statusStyles[localScriptData?.status || localLoopData?.status || 'idle']"
              >
                {{ localScriptData?.status || localLoopData?.status }}
              </span>
            </div>
          </div>
          
          <!-- Save as Template Button -->
          <div class="tw-mt-3 tw-pt-3 tw-border-t tw-border-neutral-200">
            <button
              class="tw-w-full tw-px-3 tw-py-2 tw-text-sm tw-font-medium tw-text-primary-600 tw-bg-primary-50 tw-rounded-md hover:tw-bg-primary-100 tw-transition-colors tw-flex tw-items-center tw-justify-center tw-gap-2"
              @click="openTemplateDialog"
            >
              <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7v8a2 2 0 002 2h6M8 7V5a2 2 0 012-2h4.586a1 1 0 01.707.293l4.414 4.414a1 1 0 01.293.707V15a2 2 0 01-2 2h-2M8 7H6a2 2 0 00-2 2v10a2 2 0 002 2h8a2 2 0 002-2v-2" />
              </svg>
              Save as Template
            </button>
          </div>
        </section>

        <!-- Appearance Section -->
        <section class="tw-bg-neutral-50 tw-rounded-lg tw-p-4">
          <h3 class="tw-text-xs tw-font-semibold tw-text-neutral-500 tw-uppercase tw-tracking-wide tw-mb-3">
            Appearance
          </h3>
          
          <!-- Border Color Picker -->
          <div class="tw-mb-4">
            <label class="tw-block tw-text-sm tw-font-medium tw-text-neutral-700 tw-mb-2">
              Border Color
            </label>
            <div class="tw-flex tw-flex-wrap tw-gap-2">
              <button
                v-for="color in presetColors"
                :key="color.value"
                class="tw-w-7 tw-h-7 tw-rounded-full tw-border-2 tw-transition-all hover:tw-scale-110"
                :class="currentBorderColor === color.value ? 'tw-border-neutral-800 tw-ring-2 tw-ring-neutral-400 tw-ring-offset-1' : 'tw-border-neutral-300'"
                :style="{ backgroundColor: color.value }"
                :title="color.name"
                @click="updateBorderColor(color.value)"
              />
            </div>
          </div>
          
          <!-- Background Color Picker -->
          <div class="tw-mb-4">
            <label class="tw-block tw-text-sm tw-font-medium tw-text-neutral-700 tw-mb-2">
              Background Color
            </label>
            <div class="tw-flex tw-flex-wrap tw-gap-2">
              <button
                class="tw-w-7 tw-h-7 tw-rounded-full tw-border-2 tw-transition-all hover:tw-scale-110"
                :class="currentBackgroundColor === '#ffffff' ? 'tw-border-neutral-800 tw-ring-2 tw-ring-neutral-400 tw-ring-offset-1' : 'tw-border-neutral-300'"
                style="background-color: #ffffff"
                title="White"
                @click="updateBackgroundColor('#ffffff')"
              />
              <button
                class="tw-w-7 tw-h-7 tw-rounded-full tw-border-2 tw-transition-all hover:tw-scale-110"
                :class="currentBackgroundColor === '#f9fafb' ? 'tw-border-neutral-800 tw-ring-2 tw-ring-neutral-400 tw-ring-offset-1' : 'tw-border-neutral-300'"
                style="background-color: #f9fafb"
                title="Light Gray"
                @click="updateBackgroundColor('#f9fafb')"
              />
              <button
                v-for="color in presetColors.slice(1)"
                :key="'bg-' + color.value"
                class="tw-w-7 tw-h-7 tw-rounded-full tw-border-2 tw-transition-all hover:tw-scale-110"
                :class="currentBackgroundColor === color.value ? 'tw-border-neutral-800 tw-ring-2 tw-ring-neutral-400 tw-ring-offset-1' : 'tw-border-neutral-300'"
                :style="{ backgroundColor: color.value }"
                :title="color.name"
                @click="updateBackgroundColor(color.value)"
              />
            </div>
          </div>
          
          <!-- Icon Picker (Optional) -->
          <div>
            <label class="tw-block tw-text-sm tw-font-medium tw-text-neutral-700 tw-mb-2">
              Icon (Optional)
            </label>
            <div class="tw-grid tw-grid-cols-5 tw-gap-1.5">
              <button
                v-for="iconOption in nodeIconOptions"
                :key="iconOption.name"
                class="tw-w-8 tw-h-8 tw-rounded-md tw-flex tw-items-center tw-justify-center tw-transition-all hover:tw-bg-neutral-200"
                :class="currentIcon === iconOption.icon ? 'tw-bg-primary-100 tw-text-primary-600 tw-ring-1 tw-ring-primary-400' : 'tw-bg-neutral-100 tw-text-neutral-500'"
                :title="iconOption.name"
                @click="updateNodeIcon(iconOption.icon)"
              >
                <component
                  v-if="iconOption.icon && getIconComponent(iconOption.icon)"
                  :is="getIconComponent(iconOption.icon) as any"
                  class="tw-w-4 tw-h-4"
                />
                <svg v-else class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636" />
                </svg>
              </button>
            </div>
          </div>
        </section>

        <!-- Script Step Editor -->
        <template v-if="isScriptStep && localScriptData">
          <!-- Basic Configuration -->
          <section class="tw-space-y-4">
            <h3 class="tw-text-xs tw-font-semibold tw-text-neutral-500 tw-uppercase tw-tracking-wide">
              Basic Configuration
            </h3>

            <!-- Step ID -->
            <div>
              <label class="tw-block tw-text-sm tw-font-medium tw-text-neutral-700 tw-mb-1">
                Step ID
              </label>
              <el-input
                v-model="localScriptData.stepId"
                placeholder="Enter step ID"
                size="small"
                @change="updateNodeData"
              />
            </div>

            <!-- Script Name -->
            <div>
              <label class="tw-block tw-text-sm tw-font-medium tw-text-neutral-700 tw-mb-1">
                Script Name
              </label>
              <el-input
                v-model="localScriptData.scriptName"
                placeholder="Enter script name"
                size="small"
                @change="updateNodeData"
              />
            </div>

            <!-- Script Version -->
            <div>
              <label class="tw-block tw-text-sm tw-font-medium tw-text-neutral-700 tw-mb-1">
                Script Version
              </label>
              <el-input
                v-model="localScriptData.scriptVersion"
                placeholder="e.g., 1.0.0"
                size="small"
                @change="updateNodeData"
              />
            </div>
          </section>

          <!-- Parameters -->
          <section class="tw-space-y-3">
            <h3 class="tw-text-xs tw-font-semibold tw-text-neutral-500 tw-uppercase tw-tracking-wide">
              Parameters
            </h3>

            <!-- Existing parameters -->
            <div v-if="localScriptData.params && Object.keys(localScriptData.params).length > 0" class="tw-space-y-2">
              <div 
                v-for="(value, key) in localScriptData.params" 
                :key="key"
                class="tw-flex tw-items-start tw-gap-2 tw-bg-neutral-50 tw-rounded-md tw-p-2"
              >
                <div class="tw-flex-1 tw-grid tw-grid-cols-2 tw-gap-2">
                  <el-input
                    :model-value="String(key)"
                    size="small"
                    disabled
                    class="tw-font-mono tw-text-xs"
                  />
                  <el-input
                    :model-value="String(value)"
                    size="small"
                    @change="(val: string) => { localScriptData!.params[key] = val; updateNodeData() }"
                    class="tw-font-mono tw-text-xs"
                  />
                </div>
                <button
                  class="tw-p-1.5 tw-text-neutral-400 hover:tw-text-red-500 tw-rounded hover:tw-bg-red-50 tw-transition-colors"
                  @click="removeParameter(String(key))"
                  title="Remove parameter"
                >
                  <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            </div>

            <!-- Add new parameter -->
            <div class="tw-flex tw-gap-2">
              <el-input
                v-model="newParamKey"
                placeholder="Key"
                size="small"
                class="tw-flex-1"
                @keyup.enter="addParameter"
              />
              <el-input
                v-model="newParamValue"
                placeholder="Value"
                size="small"
                class="tw-flex-1"
                @keyup.enter="addParameter"
              />
              <el-button
                size="small"
                :disabled="!newParamKey.trim()"
                @click="addParameter"
              >
                <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
                </svg>
              </el-button>
            </div>
          </section>

          <!-- Execution Settings -->
          <section class="tw-space-y-4">
            <h3 class="tw-text-xs tw-font-semibold tw-text-neutral-500 tw-uppercase tw-tracking-wide">
              Execution Settings
            </h3>

            <!-- Timeout -->
            <div>
              <label class="tw-block tw-text-sm tw-font-medium tw-text-neutral-700 tw-mb-1">
                Timeout (ms)
              </label>
              <el-input-number
                v-model="localScriptData.timeout"
                :min="0"
                :step="1000"
                size="small"
                class="tw-w-full"
                @change="updateNodeData"
              />
            </div>

            <!-- Failure Strategy -->
            <div>
              <label class="tw-block tw-text-sm tw-font-medium tw-text-neutral-700 tw-mb-1">
                On Failure
              </label>
              <el-select
                v-model="localScriptData.onFail"
                size="small"
                class="tw-w-full"
                @change="updateNodeData"
              >
                <el-option
                  v-for="option in failOptions"
                  :key="option.value"
                  :label="option.label"
                  :value="option.value"
                />
              </el-select>
            </div>

            <!-- Export Outputs -->
            <div class="tw-flex tw-items-center tw-gap-2">
              <el-checkbox
                v-model="localScriptData.exportOutputs"
                @change="updateNodeData"
              />
              <label class="tw-text-sm tw-text-neutral-700">Export outputs</label>
            </div>

            <!-- Skip Condition (optional) -->
            <div>
              <label class="tw-block tw-text-sm tw-font-medium tw-text-neutral-700 tw-mb-1">
                Skip If
                <span class="tw-text-neutral-400 tw-font-normal">(optional)</span>
              </label>
              <el-input
                v-model="localScriptData.skipIf"
                placeholder="e.g., ${scope.skip_tests} == 'true'"
                size="small"
                clearable
                @change="updateNodeData"
              />
              <p class="tw-mt-1 tw-text-xs tw-text-neutral-400">
                Expression that causes this step to be skipped if true. Supports <code>$&#123;...&#125;</code> variable references.
              </p>
            </div>
          </section>

          <!-- Resources -->
          <section class="tw-space-y-3">
            <h3 class="tw-text-xs tw-font-semibold tw-text-neutral-500 tw-uppercase tw-tracking-wide">
              Resources
            </h3>

            <!-- Existing resources -->
            <div v-if="localScriptData.resources && localScriptData.resources.length > 0" class="tw-flex tw-flex-wrap tw-gap-2">
              <span 
                v-for="(resource, index) in localScriptData.resources" 
                :key="index"
                class="tw-inline-flex tw-items-center tw-gap-1 tw-px-2 tw-py-1 tw-bg-primary-50 tw-text-primary-700 tw-rounded tw-text-xs tw-font-medium"
              >
                {{ resource }}
                <button
                  class="tw-ml-1 tw-text-primary-400 hover:tw-text-primary-600"
                  @click="removeResource(index)"
                >
                  <svg class="tw-w-3 tw-h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </span>
            </div>

            <!-- Add new resource -->
            <div class="tw-flex tw-gap-2">
              <el-input
                v-model="newResource"
                placeholder="Add resource"
                size="small"
                class="tw-flex-1"
                @keyup.enter="addResource"
              />
              <el-button
                size="small"
                :disabled="!newResource.trim()"
                @click="addResource"
              >
                <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
                </svg>
              </el-button>
            </div>
          </section>

          <!-- Script Preview -->
          <section class="tw-space-y-3">
            <h3 class="tw-text-xs tw-font-semibold tw-text-neutral-500 tw-uppercase tw-tracking-wide">
              Script Preview
            </h3>

            <!-- Loading state -->
            <div v-if="scriptPreviewLoading" class="tw-flex tw-items-center tw-justify-center tw-py-6 tw-bg-neutral-50 tw-rounded-lg">
              <svg class="tw-animate-spin tw-h-5 tw-w-5 tw-text-primary-500" fill="none" viewBox="0 0 24 24">
                <circle class="tw-opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
                <path class="tw-opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
              </svg>
              <span class="tw-ml-2 tw-text-sm tw-text-neutral-500">Loading script...</span>
            </div>

            <!-- Error state -->
            <div v-else-if="scriptPreviewError" class="tw-p-3 tw-bg-red-50 tw-rounded-lg">
              <p class="tw-text-xs tw-text-red-600">{{ scriptPreviewError }}</p>
            </div>

            <!-- Monaco preview -->
            <div v-else-if="scriptPreviewContent" class="tw-border tw-border-neutral-200 tw-rounded-lg tw-overflow-hidden">
              <div class="tw-h-40">
                <MonacoEditor
                  :model-value="scriptPreviewLines"
                  language="python"
                  :read-only="true"
                />
              </div>
              <div v-if="scriptPreviewContent.split('\n').length > 10" class="tw-px-3 tw-py-1.5 tw-bg-neutral-50 tw-border-t tw-border-neutral-200 tw-text-xs tw-text-neutral-500 tw-text-center">
                Showing first 10 of {{ scriptPreviewContent.split('\n').length }} lines
              </div>
            </div>

            <!-- No content placeholder -->
            <div v-else class="tw-p-3 tw-bg-neutral-50 tw-rounded-lg tw-text-center">
              <p class="tw-text-xs tw-text-neutral-400">No script content available</p>
            </div>

            <!-- Edit Script button -->
            <button
              v-if="resolvedScriptId"
              class="tw-w-full tw-px-4 tw-py-2.5 tw-text-sm tw-font-semibold tw-text-white tw-bg-primary-600 tw-rounded-lg hover:tw-bg-primary-700 tw-transition-colors tw-flex tw-items-center tw-justify-center tw-gap-2"
              @click="openScriptEditor"
            >
              <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4" />
              </svg>
              Edit Script
            </button>
          </section>
        </template>

        <!-- Variable Node Editor -->
        <template v-else-if="isVariable && localVariableData">
          <section class="tw-space-y-3">
            <h3 class="tw-text-xs tw-font-semibold tw-text-neutral-500 tw-uppercase tw-tracking-wide">
              Variables
            </h3>

            <!-- Existing variables -->
            <div v-if="Object.keys(localVariableData.variables).length > 0" class="tw-space-y-2">
              <div 
                v-for="(value, key) in localVariableData.variables" 
                :key="key"
                class="tw-flex tw-items-start tw-gap-2 tw-bg-neutral-50 tw-rounded-md tw-p-2"
              >
                <div class="tw-flex-1 tw-grid tw-grid-cols-2 tw-gap-2">
                  <el-input
                    :model-value="String(key)"
                    size="small"
                    disabled
                    class="tw-font-mono tw-text-xs"
                  />
                  <el-input
                    :model-value="String(value)"
                    size="small"
                    @change="(val: string) => { localVariableData!.variables[key] = val; updateNodeData() }"
                    class="tw-font-mono tw-text-xs"
                  />
                </div>
                <button
                  class="tw-p-1.5 tw-text-neutral-400 hover:tw-text-red-500 tw-rounded hover:tw-bg-red-50 tw-transition-colors"
                  @click="removeVariable(String(key))"
                  title="Remove variable"
                >
                  <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            </div>

            <!-- Add new variable -->
            <div class="tw-flex tw-gap-2">
              <el-input
                v-model="newParamKey"
                placeholder="Variable name"
                size="small"
                class="tw-flex-1"
                @keyup.enter="addVariable"
              />
              <el-input
                v-model="newParamValue"
                placeholder="Value"
                size="small"
                class="tw-flex-1"
                @keyup.enter="addVariable"
              />
              <el-button
                size="small"
                :disabled="!newParamKey.trim()"
                @click="addVariable"
              >
                <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
                </svg>
              </el-button>
            </div>
          </section>
        </template>

        <!-- Loop Container Editor -->
        <template v-else-if="isLoopContainer && localLoopData">
          <!-- Loop Configuration -->
          <section class="tw-space-y-4">
            <h3 class="tw-text-xs tw-font-semibold tw-text-neutral-500 tw-uppercase tw-tracking-wide">
              Loop Configuration
            </h3>

            <!-- Loop ID -->
            <div>
              <label class="tw-block tw-text-sm tw-font-medium tw-text-neutral-700 tw-mb-1">
                Loop ID
              </label>
              <el-input
                v-model="localLoopData.loopId"
                placeholder="Enter loop ID"
                size="small"
                @change="updateNodeData"
              />
            </div>

            <!-- Loop Type -->
            <div>
              <label class="tw-block tw-text-sm tw-font-medium tw-text-neutral-700 tw-mb-1">
                Loop Type
              </label>
              <el-select
                :model-value="localLoopData.loopType"
                size="small"
                class="tw-w-full"
                @change="updateLoopType"
              >
                <el-option label="FOR (count-based)" value="for" />
                <el-option label="WHILE (condition-based)" value="while" />
                <el-option label="FOREACH (collection-based)" value="foreach" />
              </el-select>
            </div>

            <!-- Count (for) -->
            <div v-if="localLoopData.loopType === 'for'">
              <label class="tw-block tw-text-sm tw-font-medium tw-text-neutral-700 tw-mb-1">
                Iteration Count
              </label>
              <el-input-number
                v-model="localLoopData.count"
                :min="1"
                :step="1"
                size="small"
                class="tw-w-full"
                @change="updateNodeData"
              />
            </div>

            <!-- Condition (while) -->
            <div v-if="localLoopData.loopType === 'while'">
              <label class="tw-block tw-text-sm tw-font-medium tw-text-neutral-700 tw-mb-1">
                Condition Expression
              </label>
              <el-input
                v-model="localLoopData.condition"
                placeholder="e.g., counter < 10"
                size="small"
                @change="updateNodeData"
              />
            </div>

            <!-- Iteration Variable (foreach) -->
            <div v-if="localLoopData.loopType === 'foreach'">
              <label class="tw-block tw-text-sm tw-font-medium tw-text-neutral-700 tw-mb-1">
                Iteration Variable
              </label>
              <el-input
                v-model="localLoopData.iterationVar"
                placeholder="e.g., item"
                size="small"
                @change="updateNodeData"
              />
            </div>

            <!-- Collection Expression (foreach) -->
            <div v-if="localLoopData.loopType === 'foreach'">
              <label class="tw-block tw-text-sm tw-font-medium tw-text-neutral-700 tw-mb-1">
                Collection Expression
              </label>
              <el-input
                v-model="localLoopData.collectionExpr"
                placeholder="e.g., ${items}"
                size="small"
                @change="updateNodeData"
              />
            </div>

            <!-- Filter Condition (foreach, optional) -->
            <div v-if="localLoopData.loopType === 'foreach'">
              <label class="tw-block tw-text-sm tw-font-medium tw-text-neutral-700 tw-mb-1">
                Filter Condition
                <span class="tw-text-neutral-400 tw-font-normal">(optional)</span>
              </label>
              <el-input
                v-model="localLoopData.condition"
                placeholder="e.g., item.active === true"
                size="small"
                @change="updateNodeData"
              />
            </div>
          </section>

          <!-- Execution Settings -->
          <section class="tw-space-y-4">
            <h3 class="tw-text-xs tw-font-semibold tw-text-neutral-500 tw-uppercase tw-tracking-wide">
              Execution Settings
            </h3>

            <!-- Execution Mode -->
            <div>
              <label class="tw-block tw-text-sm tw-font-medium tw-text-neutral-700 tw-mb-1">
                Execution Mode
              </label>
              <el-radio-group
                :model-value="localLoopData.executionMode"
                size="small"
                @change="updateExecutionMode"
              >
                <el-radio-button value="serial">Serial</el-radio-button>
                <el-radio-button value="parallel">Parallel</el-radio-button>
              </el-radio-group>
            </div>

            <!-- Max Concurrency (parallel only) -->
            <div v-if="localLoopData.executionMode === 'parallel'">
              <label class="tw-block tw-text-sm tw-font-medium tw-text-neutral-700 tw-mb-1">
                Max Concurrency
              </label>
              <el-input-number
                v-model="localLoopData.maxConcurrency"
                :min="2"
                :max="32"
                :step="1"
                size="small"
                class="tw-w-full"
                @change="updateNodeData"
              />
            </div>

            <!-- Skip Condition (optional) -->
            <div>
              <label class="tw-block tw-text-sm tw-font-medium tw-text-neutral-700 tw-mb-1">
                Skip If
                <span class="tw-text-neutral-400 tw-font-normal">(optional)</span>
              </label>
              <el-input
                v-model="localLoopData.skipIf"
                placeholder="e.g., ${scope.skip_tests} == 'true'"
                size="small"
                clearable
                @change="updateNodeData"
              />
              <p class="tw-mt-1 tw-text-xs tw-text-neutral-400">
                Expression that causes this loop to be skipped if true. Supports <code>$&#123;...&#125;</code> variable references.
              </p>
            </div>
          </section>

          <!-- Loop Status -->
          <section v-if="localLoopData.status" class="tw-space-y-3">
            <h3 class="tw-text-xs tw-font-semibold tw-text-neutral-500 tw-uppercase tw-tracking-wide">
              Status
            </h3>
            <div class="tw-flex tw-items-center tw-gap-2">
              <span
                class="tw-px-2 tw-py-0.5 tw-rounded-full tw-text-xs tw-font-medium"
                :class="statusStyles[localLoopData.status]"
              >
                {{ localLoopData.status }}
              </span>
            </div>
          </section>
        </template>

        <!-- Unknown node type -->
        <div v-else class="tw-text-center tw-py-8">
          <p class="tw-text-sm tw-text-neutral-500">
            No editable properties for this node type.
          </p>
        </div>
      </div>
      </template>
    </div>
    
    <!-- Template Dialog -->
    <NodeTemplateDialog
      :visible="showTemplateDialog"
      :node="selectedNode"
      @update:visible="showTemplateDialog = $event"
      @saved="handleTemplateSaved"
    />
  </div>
</template>

<style scoped>
/* Override Element Plus input styles to match design system */
:deep(.el-input__wrapper) {
  box-shadow: 0 0 0 1px var(--color-border-default) inset;
  border-radius: var(--radius-md);
}

:deep(.el-input__wrapper:hover) {
  box-shadow: 0 0 0 1px var(--color-primary) inset;
}

:deep(.el-input__wrapper.is-focus) {
  box-shadow: 0 0 0 1px var(--color-primary) inset;
}

:deep(.el-input--small .el-input__wrapper) {
  padding: 4px 8px;
}

:deep(.el-select .el-select__wrapper) {
  box-shadow: 0 0 0 1px var(--color-border-default) inset;
  border-radius: var(--radius-md);
}

:deep(.el-select .el-select__wrapper:hover) {
  box-shadow: 0 0 0 1px var(--color-primary) inset;
}

:deep(.el-select .el-select__wrapper.is-focus) {
  box-shadow: 0 0 0 1px var(--color-primary) inset;
}

:deep(.el-input-number) {
  width: 100%;
}

:deep(.el-input-number .el-input__wrapper) {
  padding-left: 8px;
  padding-right: 8px;
}

:deep(.el-checkbox__label) {
  color: var(--color-text-secondary);
}

:deep(.el-radio-group) {
  width: 100%;
}

:deep(.el-radio-button__inner) {
  padding: 5px 12px;
}

/* Override MonacoEditor min-height for inline preview */
:deep(.monaco-editor-container) {
  min-height: 0;
  height: 100%;
}
</style>
