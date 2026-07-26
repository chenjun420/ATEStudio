import { ref, readonly } from 'vue'
import type { Node } from '@antv/x6'
import {
  listNodeTemplates,
  createNodeTemplate,
  deleteNodeTemplate,
  type NodeTemplate,
  type NodeTemplateCreate,
} from '@/api/nodeTemplates'
import type { NodeData } from '@/models/nodes/types'

/**
 * Template data extracted from a node
 */
export interface NodeTemplateData {
  name: string
  type: string
  appearance?: Record<string, unknown>
  default_data?: Record<string, unknown>
}

/**
 * Result of applying a template to a node
 */
export interface ApplyTemplateResult {
  success: boolean
  error?: string
}

/**
 * Composable state
 */
const templates = ref<NodeTemplate[]>([])
const isLoading = ref(false)
const isLoaded = ref(false)
const error = ref<string | null>(null)

/**
 * Extract template data from a node
 */
function extractTemplateData(node: Node, templateName: string): NodeTemplateCreate {
  const data = node.getData() as NodeData

  // Cast to unknown first to safely handle different NodeData types
  const dataRecord = data as unknown as Record<string, unknown>

  // Extract node type from shape or data
  const type = dataRecord?.nodeType || 'script-step'

  // Extract appearance (position, size, styling)
  const position = node.getPosition()
  const size = node.getSize()
  const appearance: Record<string, unknown> = {
    x: position.x,
    y: position.y,
    width: size.width,
    height: size.height,
  }

  // Extract default_data from node data, excluding runtime state
  const defaultData: Record<string, unknown> = {}
  const runtimeFields = ['status', 'lastRunTime', 'errorMessage']

  for (const [key, value] of Object.entries(dataRecord)) {
    if (!runtimeFields.includes(key)) {
      defaultData[key] = value
    }
  }

  return {
    name: templateName,
    type: String(type),
    appearance,
    default_data: Object.keys(defaultData).length > 0 ? defaultData : undefined,
  }
}

/**
 * Apply template data to a node
 */
function applyTemplateToNode(node: Node, template: NodeTemplate): ApplyTemplateResult {
  try {
    // Apply default_data to node
    if (template.default_data) {
      const currentData = node.getData() as Record<string, unknown>
      const newData = {
        ...currentData,
        ...template.default_data,
      }
      node.setData(newData)
    }

    // Apply appearance if specified
    if (template.appearance) {
      const { x, y, width, height } = template.appearance as Record<string, unknown>

      if (typeof x === 'number' && typeof y === 'number') {
        node.setPosition(x, y)
      }

      if (typeof width === 'number' && typeof height === 'number') {
        node.setSize(width, height)
      }
    }

    return { success: true }
  } catch (err) {
    const errorMessage = err instanceof Error ? err.message : 'Unknown error'
    return { success: false, error: errorMessage }
  }
}

/**
 * Composable for managing node templates
 */
export function useNodeTemplate() {
  /**
   * Load templates from API with caching
   */
  async function loadTemplates(forceRefresh = false): Promise<NodeTemplate[]> {
    // Return cached data if already loaded and not forcing refresh
    if (isLoaded.value && !forceRefresh && templates.value.length > 0) {
      return templates.value
    }

    isLoading.value = true
    error.value = null

    try {
      const result = await listNodeTemplates()
      templates.value = result
      isLoaded.value = true
      return result
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : 'Failed to load templates'
      error.value = errorMessage
      throw err
    } finally {
      isLoading.value = false
    }
  }

  /**
   * Create a template from a node
   */
  async function createTemplateFromNode(
    node: Node,
    templateName: string
  ): Promise<NodeTemplate> {
    isLoading.value = true
    error.value = null

    try {
      const templateData = extractTemplateData(node, templateName)
      const newTemplate = await createNodeTemplate(templateData)

      // Add to local cache
      templates.value.push(newTemplate)

      return newTemplate
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : 'Failed to create template'
      error.value = errorMessage
      throw err
    } finally {
      isLoading.value = false
    }
  }

  /**
   * Apply a template to a node
   */
  function applyTemplate(node: Node, template: NodeTemplate): ApplyTemplateResult {
    return applyTemplateToNode(node, template)
  }

  /**
   * Delete a template by ID
   */
  async function deleteTemplate(id: string): Promise<void> {
    isLoading.value = true
    error.value = null

    try {
      await deleteNodeTemplate(id)

      // Remove from local cache
      templates.value = templates.value.filter(t => t.id !== id)
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : 'Failed to delete template'
      error.value = errorMessage
      throw err
    } finally {
      isLoading.value = false
    }
  }

  /**
   * Get a template by ID from cache
   */
  function getTemplateById(id: string): NodeTemplate | undefined {
    return templates.value.find(t => t.id === id)
  }

  /**
   * Get templates by type from cache
   */
  function getTemplatesByType(type: string): NodeTemplate[] {
    return templates.value.filter(t => t.type === type)
  }

  /**
   * Clear the template cache
   */
  function clearCache(): void {
    templates.value = []
    isLoaded.value = false
    error.value = null
  }

  return {
    // State (readonly to prevent direct mutation)
    templates: readonly(templates),
    isLoading: readonly(isLoading),
    isLoaded: readonly(isLoaded),
    error: readonly(error),

    // Actions
    loadTemplates,
    createTemplateFromNode,
    applyTemplate,
    deleteTemplate,

    // Helpers
    getTemplateById,
    getTemplatesByType,
    clearCache,
  }
}
