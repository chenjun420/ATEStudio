import { ref, computed, type Ref } from 'vue'
import type { Graph, Node } from '@antv/x6'
import type { ScriptStepData } from '@/models/nodes/types'

/**
 * Common editable properties for batch editing
 */
export interface BatchEditProperties {
  timeout?: number
  onFail?: 'stop' | 'skip' | 'ignore'
  exportOutputs?: boolean
}

/**
 * Result of batch edit operation
 */
export interface BatchEditResult {
  success: boolean
  updatedCount: number
  errors: Array<{ nodeId: string; error: string }>
}

/**
 * Composable for batch editing multiple nodes
 */
export function useBatchEdit(graphInstance: Ref<Graph | null>) {
  // Selected node IDs
  const selectedNodeIds = ref<Set<string>>(new Set())

  // Computed: get selected nodes
  const selectedNodes = computed<Node[]>(() => {
    if (!graphInstance.value) return []

    const nodes: Node[] = []
    selectedNodeIds.value.forEach(id => {
      const cell = graphInstance.value!.getCellById(id)
      if (cell && cell.isNode()) {
        nodes.push(cell as Node)
      }
    })

    return nodes
  })

  // Computed: check if batch mode is active
  const isBatchMode = computed(() => selectedNodeIds.value.size > 1)

  // Computed: check if any nodes are selected
  const hasSelection = computed(() => selectedNodeIds.value.size > 0)

  // Computed: get selection count
  const selectionCount = computed(() => selectedNodeIds.value.size)

  /**
   * Get common properties across selected nodes
   */
  function getCommonProperties(): Partial<BatchEditProperties> {
    const nodes = selectedNodes.value
    if (nodes.length === 0) return {}

    // Filter to only script-step nodes
    const scriptNodes = nodes.filter(node => {
      const data = node.getData() as Record<string, unknown>
      return data?.nodeType === 'script-step' || node.shape === 'script-step-node'
    })

    if (scriptNodes.length === 0) return {}

    // Find common values
    const firstNodeData = scriptNodes[0].getData() as ScriptStepData
    const commonProps: Partial<BatchEditProperties> = {}

    // Check timeout
    const firstTimeout = firstNodeData.timeout
    const allHaveSameTimeout = scriptNodes.every(node => {
      const data = node.getData() as ScriptStepData
      return data.timeout === firstTimeout
    })
    if (allHaveSameTimeout) {
      commonProps.timeout = firstTimeout
    }

    // Check onFail
    const firstOnFail = firstNodeData.onFail
    const allHaveSameOnFail = scriptNodes.every(node => {
      const data = node.getData() as ScriptStepData
      return data.onFail === firstOnFail
    })
    if (allHaveSameOnFail) {
      commonProps.onFail = firstOnFail
    }

    // Check exportOutputs
    const firstExport = firstNodeData.exportOutputs
    const allHaveSameExport = scriptNodes.every(node => {
      const data = node.getData() as ScriptStepData
      return data.exportOutputs === firstExport
    })
    if (allHaveSameExport) {
      commonProps.exportOutputs = firstExport
    }

    return commonProps
  }

  /**
   * Check if a property is common across all selected nodes
   */
  function isPropertyCommon(property: keyof BatchEditProperties): boolean {
    const nodes = selectedNodes.value
    if (nodes.length === 0) return false

    const scriptNodes = nodes.filter(node => {
      const data = node.getData() as Record<string, unknown>
      return data?.nodeType === 'script-step' || node.shape === 'script-step-node'
    })

    if (scriptNodes.length === 0) return false

    // Get first value
    const firstData = scriptNodes[0].getData() as ScriptStepData
    const firstValue = firstData[property as keyof ScriptStepData]

    // Check if all nodes have the same value
    return scriptNodes.every(node => {
      const data = node.getData() as ScriptStepData
      return data[property as keyof ScriptStepData] === firstValue
    })
  }

  /**
   * Apply batch edits to all selected nodes
   */
  function applyBatchEdits(properties: BatchEditProperties): BatchEditResult {
    const nodes = selectedNodes.value
    const result: BatchEditResult = {
      success: true,
      updatedCount: 0,
      errors: [],
    }

    nodes.forEach(node => {
      try {
        const data = node.getData() as Record<string, unknown>
        
        // Only update script-step nodes for now
        if (data?.nodeType !== 'script-step' && node.shape !== 'script-step-node') {
          return
        }

        const updatedData = {
          ...data,
          ...(properties.timeout !== undefined && { timeout: properties.timeout }),
          ...(properties.onFail !== undefined && { onFail: properties.onFail }),
          ...(properties.exportOutputs !== undefined && { exportOutputs: properties.exportOutputs }),
        } as Record<string, unknown>

        node.setData(updatedData, { silent: false })
        result.updatedCount++
      } catch (err) {
        const errorMessage = err instanceof Error ? err.message : 'Unknown error'
        result.errors.push({
          nodeId: node.id,
          error: errorMessage,
        })
        result.success = false
      }
    })

    return result
  }

  /**
   * Delete all selected nodes
   */
  function batchDelete(): { success: boolean; deletedCount: number; errors: string[] } {
    if (!graphInstance.value) {
      return { success: false, deletedCount: 0, errors: ['Graph instance not available'] }
    }

    const nodes = selectedNodes.value
    const errors: string[] = []
    let deletedCount = 0

    nodes.forEach(node => {
      try {
        graphInstance.value!.removeCell(node)
        deletedCount++
      } catch (err) {
        const errorMessage = err instanceof Error ? err.message : 'Unknown error'
        errors.push(`Failed to delete node ${node.id}: ${errorMessage}`)
      }
    })

    // Clear selection
    clearSelection()

    return {
      success: errors.length === 0,
      deletedCount,
      errors,
    }
  }

  /**
   * Add a node to selection
   */
  function addToSelection(nodeId: string) {
    selectedNodeIds.value.add(nodeId)
  }

  /**
   * Remove a node from selection
   */
  function removeFromSelection(nodeId: string) {
    selectedNodeIds.value.delete(nodeId)
  }

  /**
   * Toggle node selection
   */
  function toggleSelection(nodeId: string) {
    if (selectedNodeIds.value.has(nodeId)) {
      selectedNodeIds.value.delete(nodeId)
    } else {
      selectedNodeIds.value.add(nodeId)
    }
  }

  /**
   * Check if a node is selected
   */
  function isSelected(nodeId: string): boolean {
    return selectedNodeIds.value.has(nodeId)
  }

  /**
   * Clear all selections
   */
  function clearSelection() {
    selectedNodeIds.value.clear()
  }

  /**
   * Select multiple nodes
   */
  function selectMultiple(nodeIds: string[]) {
    clearSelection()
    nodeIds.forEach(id => selectedNodeIds.value.add(id))
  }

  return {
    // State
    selectedNodeIds,
    selectedNodes,

    // Computed
    isBatchMode,
    hasSelection,
    selectionCount,

    // Actions
    getCommonProperties,
    isPropertyCommon,
    applyBatchEdits,
    batchDelete,

    // Selection management
    addToSelection,
    removeFromSelection,
    toggleSelection,
    isSelected,
    clearSelection,
    selectMultiple,
  }
}