<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElInput, ElScrollbar, ElTag, ElEmpty } from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import { fetchScripts, type Script } from '@/api/scripts'
import { useNodeTemplate } from '@/composables/useNodeTemplate'
import type { NodeTemplate } from '@/api/nodeTemplates'
import { createDefaultLoopContainerData, type LoopContainerData } from '@/models/nodes/types'

// Tab state
const activeTab = ref<'scripts' | 'templates' | 'loops'>('scripts')

// Script data
const scripts = ref<Script[]>([])
const isLoadingScripts = ref(false)
const scriptsError = ref<string | null>(null)

// Template data
const { templates, isLoading: isLoadingTemplates, loadTemplates } = useNodeTemplate()

// Loop templates (static, built-in)
interface LoopTemplate {
  id: string
  name: string
  loopType: LoopContainerData['loopType']
  description: string
  icon: string
}

const loopTemplates: LoopTemplate[] = [
  {
    id: 'loop-for',
    name: 'FOR Loop',
    loopType: 'for',
    description: 'Count-based iteration with a fixed number of cycles',
    icon: '🔄',
  },
  {
    id: 'loop-while',
    name: 'WHILE Loop',
    loopType: 'while',
    description: 'Condition-based iteration that runs while a condition is true',
    icon: '🔁',
  },
  {
    id: 'loop-foreach',
    name: 'FOREACH Loop',
    loopType: 'foreach',
    description: 'Collection-based iteration over items in a list or array',
    icon: '🔃',
  },
]

// Filtered loop templates
const filteredLoopTemplates = computed(() => {
  if (!searchQuery.value.trim()) return loopTemplates
  const query = searchQuery.value.toLowerCase()
  return loopTemplates.filter(t =>
    t.name.toLowerCase().includes(query) ||
    t.description.toLowerCase().includes(query) ||
    t.loopType.toLowerCase().includes(query)
  )
})

// Total loops count
const totalLoops = computed(() => filteredLoopTemplates.value.length)

// Search and filter
const searchQuery = ref('')
const expandedCategories = ref(new Set<string>())

// Filter scripts by search query
const filteredScripts = computed(() => {
  if (!searchQuery.value.trim()) return scripts.value
  
  const query = searchQuery.value.toLowerCase()
  return scripts.value.filter(script => 
    script.name.toLowerCase().includes(query) ||
    script.description?.toLowerCase().includes(query) ||
    script.tags?.some(tag => tag.toLowerCase().includes(query))
  )
})

// Group scripts by category
const scriptCategories = computed(() => {
  const filtered = filteredScripts.value
  const categoryMap = new Map<string, Script[]>()
  
  // Group by category (use 'Uncategorized' if no category)
  filtered.forEach(script => {
    const category = script.category || 'Uncategorized'
    if (!categoryMap.has(category)) {
      categoryMap.set(category, [])
    }
    categoryMap.get(category)!.push(script)
  })
  
  // Convert to array and sort
  return Array.from(categoryMap.entries())
    .map(([name, items]) => ({ name, scripts: items, count: items.length }))
    .sort((a, b) => a.name.localeCompare(b.name))
})

// Total filtered count
const totalFiltered = computed(() => filteredScripts.value.length)

// Filtered templates
const filteredTemplates = computed(() => {
  if (!searchQuery.value.trim()) return templates.value
  
  const query = searchQuery.value.toLowerCase()
  return templates.value.filter(template =>
    template.name.toLowerCase().includes(query) ||
    template.type.toLowerCase().includes(query)
  )
})

// Total templates count
const totalTemplates = computed(() => filteredTemplates.value.length)

// Loading state
const isLoading = computed(() => isLoadingScripts.value || isLoadingTemplates.value)

// Load scripts on mount
onMounted(async () => {
  await loadScripts()
  await loadTemplates()
})

/**
 * Load scripts from API
 */
async function loadScripts() {
  isLoadingScripts.value = true
  scriptsError.value = null
  
  try {
    scripts.value = await fetchScripts()
    
    // Expand all categories by default
    scriptCategories.value.forEach(cat => {
      expandedCategories.value.add(cat.name)
    })
  } catch (err) {
    scriptsError.value = err instanceof Error ? err.message : 'Failed to load scripts'
    console.error('Failed to load scripts:', err)
  } finally {
    isLoadingScripts.value = false
  }
}

/**
 * Toggle category expansion
 */
function toggleCategory(name: string) {
  if (expandedCategories.value.has(name)) {
    expandedCategories.value.delete(name)
  } else {
    expandedCategories.value.add(name)
  }
}

/**
 * Handle drag start for template - set template data for drop
 */
function onTemplateDragStart(event: DragEvent, template: NodeTemplate) {
  if (!event.dataTransfer) return
  
  // Set template data as JSON for drop handler
  const templateData = {
    type: 'template',
    templateId: template.id,
    templateName: template.name,
    templateType: template.type,
    defaultData: template.default_data || {},
    appearance: template.appearance || {},
  }
  
  event.dataTransfer.setData('application/json', JSON.stringify(templateData))
  event.dataTransfer.setData('text/plain', template.name) // Fallback
  event.dataTransfer.effectAllowed = 'copy'
  
  // Add visual feedback
  const target = event.target as HTMLElement
  target.style.opacity = '0.6'
}

/**
 * Handle drag start - set script data for drop
 */
function onDragStart(event: DragEvent, script: Script) {
  if (!event.dataTransfer) return
  
  // Set script data as JSON for drop handler
  const scriptData = {
    type: 'script',
    scriptId: script.id,
    scriptName: script.name,
    scriptVersion: script.version,
    params: script.params || [],
  }
  
  event.dataTransfer.setData('application/json', JSON.stringify(scriptData))
  event.dataTransfer.setData('text/plain', script.name) // Fallback
  event.dataTransfer.effectAllowed = 'copy'
  
  // Add visual feedback
  const target = event.target as HTMLElement
  target.style.opacity = '0.6'
}

/**
 * Handle drag end - reset visual state
 */
function onDragEnd(event: DragEvent) {
  const target = event.target as HTMLElement
  target.style.opacity = '1'
}

/**
 * Handle drag start for loop template - set loop data for drop
 */
function onLoopDragStart(event: DragEvent, loopTemplate: LoopTemplate) {
  if (!event.dataTransfer) return

  // Create default loop data with the appropriate loop type
  const defaultData = createDefaultLoopContainerData()
  defaultData.loopType = loopTemplate.loopType
  defaultData.loopId = `loop-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`

  // Set loop data as JSON for drop handler using 'template' type
  // so GraphContainer can handle it via the existing template drop path
  const loopData = {
    type: 'template',
    templateId: loopTemplate.id,
    templateName: loopTemplate.name,
    templateType: 'loop-container',
    defaultData: { ...defaultData },
    appearance: {},
  }

  event.dataTransfer.setData('application/json', JSON.stringify(loopData))
  event.dataTransfer.setData('text/plain', loopTemplate.name) // Fallback
  event.dataTransfer.effectAllowed = 'copy'

  // Add visual feedback
  const target = event.target as HTMLElement
  target.style.opacity = '0.6'
}

/**
 * Get category icon based on category name
 */
function getCategoryIcon(categoryName: string): string {
  const icons: Record<string, string> = {
    'Measurements': '⚡',
    'Control': '🔌',
    'Power': '🔋',
    'Communication': '📡',
    'Data': '📊',
    'Utilities': '🔧',
    'Uncategorized': '📁',
  }
  return icons[categoryName] || '📁'
}
</script>

<template>
  <div class="step-library">
    <!-- Panel header -->
    <div class="panel-header">
      <h2 class="panel-title">Step Library</h2>
    </div>
    
    <!-- Tabs -->
    <div class="tabs-container">
      <button
        class="tab-button"
        :class="{ active: activeTab === 'scripts' }"
        @click="activeTab = 'scripts'"
      >
        <span class="tab-icon">📜</span>
        <span class="tab-label">Scripts</span>
        <span class="tab-count">{{ totalFiltered }}</span>
      </button>
      <button
        class="tab-button"
        :class="{ active: activeTab === 'templates' }"
        @click="activeTab = 'templates'"
      >
        <span class="tab-icon">📋</span>
        <span class="tab-label">Templates</span>
        <span class="tab-count">{{ totalTemplates }}</span>
      </button>
      <button
        class="tab-button"
        :class="{ active: activeTab === 'loops' }"
        @click="activeTab = 'loops'"
      >
        <span class="tab-icon">🔄</span>
        <span class="tab-label">Loops</span>
        <span class="tab-count">{{ totalLoops }}</span>
      </button>
    </div>
    
    <!-- Search input -->
    <div class="search-container">
      <ElInput
        v-model="searchQuery"
        :placeholder="activeTab === 'scripts' ? 'Search scripts...' : activeTab === 'templates' ? 'Search templates...' : 'Search loops...'"
        :prefix-icon="Search"
        clearable
        size="default"
        class="search-input"
      />
    </div>
    
    <!-- Scripts Tab Content -->
    <template v-if="activeTab === 'scripts'">
    <!-- Loading state -->
    <div v-if="isLoading" class="loading-container">
      <div class="spinner" />
      <span class="loading-text">Loading scripts...</span>
    </div>
    
    <!-- Error state -->
    <ElEmpty v-else-if="error" :description="error" />
    
    <!-- Empty state -->
    <ElEmpty v-else-if="scriptCategories.length === 0" description="No scripts found">
      <template #image>
        <span class="empty-icon">📭</span>
      </template>
    </ElEmpty>
    
    <!-- Script categories and items -->
    <ElScrollbar v-else class="script-list">
      <div class="categories">
        <div
          v-for="category in scriptCategories"
          :key="category.name"
          class="category"
        >
          <!-- Category header -->
          <button
            class="category-header"
            :class="{ expanded: expandedCategories.has(category.name) }"
            @click="toggleCategory(category.name)"
          >
            <span class="category-icon">{{ getCategoryIcon(category.name) }}</span>
            <span class="category-name">{{ category.name }}</span>
            <span class="category-count">{{ category.count }}</span>
            <span class="category-toggle">{{ expandedCategories.has(category.name) ? '−' : '+' }}</span>
          </button>
          
          <!-- Scripts in category -->
          <div
            v-show="expandedCategories.has(category.name)"
            class="category-items"
          >
            <div
              v-for="script in category.scripts"
              :key="script.id"
              class="script-item"
              draggable="true"
              @dragstart="onDragStart($event, script)"
              @dragend="onDragEnd"
            >
              <div class="script-main">
                <span class="script-name">{{ script.name }}</span>
                <span v-if="script.version" class="script-version">v{{ script.version }}</span>
              </div>
              <p v-if="script.description" class="script-description">{{ script.description }}</p>
              <div v-if="script.tags?.length" class="script-tags">
                <ElTag
                  v-for="tag in script.tags.slice(0, 3)"
                  :key="tag"
                  size="small"
                  effect="plain"
                  class="tag"
                >
                  {{ tag }}
                </ElTag>
                <span v-if="script.tags.length > 3" class="tag-more">+{{ script.tags.length - 3 }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </ElScrollbar>
    </template>
    
    <!-- Templates Tab Content -->
    <template v-else-if="activeTab === 'templates'">
      <!-- Loading state -->
      <div v-if="isLoading" class="loading-container">
        <div class="spinner" />
        <span class="loading-text">Loading templates...</span>
      </div>
      
      <!-- Error state -->
      <ElEmpty v-else-if="scriptsError" :description="scriptsError" />
      
      <!-- Empty state -->
      <ElEmpty v-else-if="filteredTemplates.length === 0" description="No templates found">
        <template #image>
          <span class="empty-icon">📭</span>
        </template>
      </ElEmpty>
      
      <!-- Templates list -->
      <ElScrollbar v-else class="script-list">
        <div class="templates-container">
          <div
            v-for="template in filteredTemplates"
            :key="template.id"
            class="template-item"
            draggable="true"
            @dragstart="onTemplateDragStart($event, template)"
            @dragend="onDragEnd"
          >
            <div class="template-header">
              <span class="template-icon">📋</span>
              <span class="template-name">{{ template.name }}</span>
            </div>
            <div class="template-meta">
              <span class="template-type">{{ template.type.replace(/-/g, ' ') }}</span>
            </div>
            <div v-if="template.default_data && Object.keys(template.default_data).length > 0" class="template-data-preview">
              <span class="preview-label">{{ Object.keys(template.default_data).length }} properties</span>
            </div>
          </div>
        </div>
      </ElScrollbar>
    </template>
    
    <!-- Loops Tab Content -->
    <template v-else-if="activeTab === 'loops'">
      <ElScrollbar class="script-list">
        <div class="loops-container">
          <div
            v-for="loopTemplate in filteredLoopTemplates"
            :key="loopTemplate.id"
            class="loop-item"
            draggable="true"
            @dragstart="onLoopDragStart($event, loopTemplate)"
            @dragend="onDragEnd"
          >
            <div class="loop-header">
              <span class="loop-icon">{{ loopTemplate.icon }}</span>
              <span class="loop-name">{{ loopTemplate.name }}</span>
            </div>
            <p class="loop-description">{{ loopTemplate.description }}</p>
            <div class="loop-meta">
              <span class="loop-type-badge">{{ loopTemplate.loopType.toUpperCase() }}</span>
            </div>
          </div>
        </div>
      </ElScrollbar>
    </template>
  </div>
</template>

<style scoped>
.step-library {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--color-bg-primary, #fff);
}

.panel-header {
  padding: var(--spacing-md, 1rem);
  border-bottom: 1px solid var(--color-border-default, #e5e7eb);
}

.panel-title {
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--color-text-primary, #111827);
  margin: 0;
}

.tabs-container {
  display: flex;
  gap: var(--spacing-xs, 0.25rem);
  padding: var(--spacing-sm, 0.5rem) var(--spacing-md, 1rem);
  border-bottom: 1px solid var(--color-border-default, #e5e7eb);
}

.tab-button {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--spacing-xs, 0.25rem);
  padding: var(--spacing-sm, 0.5rem);
  background: transparent;
  border: none;
  border-radius: var(--radius-md, 0.375rem);
  cursor: pointer;
  transition: all var(--transition-fast, 150ms ease);
}

.tab-button:hover {
  background: var(--color-bg-secondary, #f9fafb);
}

.tab-button.active {
  background: var(--color-bg-tertiary, #f3f4f6);
  color: var(--color-primary, #8b5cf6);
}

.tab-icon {
  font-size: 1rem;
}

.tab-label {
  font-size: 0.8125rem;
  font-weight: 500;
  color: var(--color-text-primary, #111827);
}

.tab-button.active .tab-label {
  color: var(--color-primary, #8b5cf6);
}

.tab-count {
  font-size: 0.75rem;
  padding: 0.125rem 0.375rem;
  background: var(--color-bg-secondary, #f9fafb);
  border-radius: var(--radius-sm, 0.25rem);
  color: var(--color-text-tertiary, #9ca3af);
}

.tab-button.active .tab-count {
  background: var(--color-primary, #8b5cf6);
  color: white;
}

.panel-subtitle {
  font-size: 0.75rem;
  color: var(--color-text-tertiary, #9ca3af);
  margin: 0.25rem 0 0;
}

.search-container {
  padding: var(--spacing-sm, 0.5rem) var(--spacing-md, 1rem);
  border-bottom: 1px solid var(--color-border-default, #e5e7eb);
}

.search-input {
  width: 100%;
}

.loading-container {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: var(--spacing-xl, 2rem);
  gap: var(--spacing-sm, 0.5rem);
}

.spinner {
  width: 24px;
  height: 24px;
  border: 2px solid var(--color-border-default, #e5e7eb);
  border-top-color: var(--color-primary, #8b5cf6);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

.loading-text {
  font-size: 0.875rem;
  color: var(--color-text-secondary, #4b5563);
}

.script-list {
  flex: 1;
  min-height: 0;
}

.categories {
  padding: var(--spacing-sm, 0.5rem);
}

.category {
  margin-bottom: var(--spacing-xs, 0.25rem);
}

.category-header {
  display: flex;
  align-items: center;
  width: 100%;
  padding: var(--spacing-sm, 0.5rem) var(--spacing-md, 1rem);
  background: var(--color-bg-secondary, #f9fafb);
  border: none;
  border-radius: var(--radius-md, 0.375rem);
  cursor: pointer;
  transition: background-color var(--transition-fast, 150ms ease);
}

.category-header:hover {
  background: var(--color-bg-tertiary, #f3f4f6);
}

.category-header.expanded {
  background: var(--color-bg-tertiary, #f3f4f6);
}

.category-icon {
  font-size: 1rem;
  margin-right: var(--spacing-sm, 0.5rem);
}

.category-name {
  flex: 1;
  font-size: 0.8125rem;
  font-weight: 500;
  color: var(--color-text-primary, #111827);
  text-align: left;
}

.category-count {
  font-size: 0.75rem;
  color: var(--color-text-tertiary, #9ca3af);
  margin-right: var(--spacing-sm, 0.5rem);
}

.category-toggle {
  font-size: 0.875rem;
  color: var(--color-text-tertiary, #9ca3af);
  font-weight: 600;
}

.category-items {
  padding: var(--spacing-xs, 0.25rem) 0;
}

.script-item {
  padding: var(--spacing-sm, 0.5rem) var(--spacing-md, 1rem);
  margin: var(--spacing-xs, 0.25rem) 0;
  background: var(--color-bg-primary, #fff);
  border: 1px solid var(--color-border-default, #e5e7eb);
  border-radius: var(--radius-lg, 0.5rem);
  cursor: grab;
  transition: all var(--transition-fast, 150ms ease);
}

.script-item:hover {
  border-color: var(--color-primary, #8b5cf6);
  box-shadow: 0 2px 4px rgba(139, 92, 246, 0.1);
}

.script-item:active {
  cursor: grabbing;
  transform: scale(0.98);
}

.script-main {
  display: flex;
  align-items: baseline;
  gap: var(--spacing-sm, 0.5rem);
}

.script-name {
  font-size: 0.8125rem;
  font-weight: 500;
  color: var(--color-text-primary, #111827);
}

.script-version {
  font-size: 0.6875rem;
  color: var(--color-text-tertiary, #9ca3af);
  padding: 0.125rem 0.375rem;
  background: var(--color-bg-tertiary, #f3f4f6);
  border-radius: var(--radius-sm, 0.25rem);
}

.script-description {
  font-size: 0.75rem;
  color: var(--color-text-secondary, #4b5563);
  margin: 0.25rem 0 0;
  line-height: 1.4;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.script-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem;
  margin-top: var(--spacing-xs, 0.25rem);
}

.tag {
  font-size: 0.625rem;
  padding: 0.125rem 0.375rem;
  height: auto;
  line-height: 1.2;
}

.tag-more {
  font-size: 0.625rem;
  color: var(--color-text-tertiary, #9ca3af);
  padding: 0.125rem 0.25rem;
}

.empty-icon {
  font-size: 3rem;
  opacity: 0.5;
}

.templates-container {
  padding: var(--spacing-sm, 0.5rem);
}

.template-item {
  padding: var(--spacing-sm, 0.5rem) var(--spacing-md, 1rem);
  margin: var(--spacing-xs, 0.25rem) 0;
  background: var(--color-bg-primary, #fff);
  border: 1px solid var(--color-border-default, #e5e7eb);
  border-radius: var(--radius-lg, 0.5rem);
  cursor: grab;
  transition: all var(--transition-fast, 150ms ease);
}

.template-item:hover {
  border-color: var(--color-primary, #8b5cf6);
  box-shadow: 0 2px 4px rgba(139, 92, 246, 0.1);
}

.template-item:active {
  cursor: grabbing;
  transform: scale(0.98);
}

.template-header {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm, 0.5rem);
  margin-bottom: var(--spacing-xs, 0.25rem);
}

.template-icon {
  font-size: 1rem;
}

.template-name {
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--color-text-primary, #111827);
}

.template-meta {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm, 0.5rem);
}

.template-type {
  font-size: 0.6875rem;
  color: var(--color-text-tertiary, #9ca3af);
  padding: 0.125rem 0.375rem;
  background: var(--color-bg-tertiary, #f3f4f6);
  border-radius: var(--radius-sm, 0.25rem);
  text-transform: capitalize;
}

.template-data-preview {
  margin-top: var(--spacing-xs, 0.25rem);
}

.preview-label {
  font-size: 0.625rem;
  color: var(--color-primary, #8b5cf6);
  padding: 0.125rem 0.375rem;
  background: rgba(139, 92, 246, 0.1);
  border-radius: var(--radius-sm, 0.25rem);
}

/* Loop items */
.loops-container {
  padding: var(--spacing-sm, 0.5rem);
}

.loop-item {
  padding: var(--spacing-sm, 0.5rem) var(--spacing-md, 1rem);
  margin: var(--spacing-xs, 0.25rem) 0;
  background: var(--color-bg-primary, #fff);
  border: 1px solid var(--color-border-default, #e5e7eb);
  border-radius: var(--radius-lg, 0.5rem);
  cursor: grab;
  transition: all var(--transition-fast, 150ms ease);
}

.loop-item:hover {
  border-color: var(--color-warning, #f59e0b);
  box-shadow: 0 2px 4px rgba(245, 158, 11, 0.1);
}

.loop-item:active {
  cursor: grabbing;
  transform: scale(0.98);
}

.loop-header {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm, 0.5rem);
  margin-bottom: var(--spacing-xs, 0.25rem);
}

.loop-icon {
  font-size: 1rem;
}

.loop-name {
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--color-text-primary, #111827);
}

.loop-description {
  font-size: 0.75rem;
  color: var(--color-text-secondary, #4b5563);
  margin: 0.25rem 0 0;
  line-height: 1.4;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.loop-meta {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm, 0.5rem);
  margin-top: var(--spacing-xs, 0.25rem);
}

.loop-type-badge {
  font-size: 0.625rem;
  font-weight: 700;
  color: var(--color-warning, #f59e0b);
  padding: 0.125rem 0.5rem;
  background: rgba(245, 158, 11, 0.1);
  border-radius: var(--radius-sm, 0.25rem);
  letter-spacing: 0.05em;
}
</style>