<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElInput, ElScrollbar, ElEmpty, ElMessageBox, ElMessage } from 'element-plus'
import { Search, DocumentCopy, Delete } from '@element-plus/icons-vue'
import { fetchSequences, deleteSequence, createSequence, type Sequence } from '@/api/sequences'
import { useTabsStore } from '@/stores/tabs'
import { useAuth } from '@/composables/useAuth'

const { hasScope } = useAuth()

const emit = defineEmits<{
  sequenceSelected: [sequence: Sequence]
  sequenceCreated: [sequence: Sequence]
}>()

const tabsStore = useTabsStore()

const sequences = ref<Sequence[]>([])
const isLoading = ref(false)
const error = ref<string | null>(null)
const searchQuery = ref('')
const contextMenuVisible = ref(false)
const contextMenuPosition = ref({ x: 0, y: 0 })
const selectedSequence = ref<Sequence | null>(null)

const filteredSequences = computed(() => {
  if (!searchQuery.value.trim()) return sequences.value
  
  const query = searchQuery.value.toLowerCase()
  return sequences.value.filter(seq =>
    seq.name.toLowerCase().includes(query) ||
    seq.description?.toLowerCase().includes(query) ||
    seq.tags?.some(tag => tag.toLowerCase().includes(query))
  )
})

const totalCount = computed(() => sequences.value.length)
const filteredCount = computed(() => filteredSequences.value.length)

onMounted(async () => {
  await loadSequences()
})

async function loadSequences() {
  isLoading.value = true
  error.value = null
  
  try {
    sequences.value = await fetchSequences()
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Failed to load sequences'
    console.error('Failed to load sequences:', err)
  } finally {
    isLoading.value = false
  }
}

function handleDoubleClick(sequence: Sequence) {
  tabsStore.addTab(sequence.id, sequence.name)
  emit('sequenceSelected', sequence)
}

function handleContextMenu(event: MouseEvent, sequence: Sequence) {
  event.preventDefault()
  
  selectedSequence.value = sequence
  contextMenuPosition.value = { x: event.clientX, y: event.clientY }
  contextMenuVisible.value = true
  
  document.addEventListener('click', closeContextMenu, { once: true })
}

function closeContextMenu() {
  contextMenuVisible.value = false
  selectedSequence.value = null
}

async function handleClone() {
  if (!selectedSequence.value) return
  
  const original = selectedSequence.value
  
  try {
    const cloned = await createSequence({
      name: `${original.name} (Copy)`,
      description: original.description,
      version: original.version,
      yaml_content: original.yaml_content,
      tags: original.tags,
    })
    
    sequences.value.push(cloned)
    tabsStore.addTab(cloned.id, cloned.name)
    
    emit('sequenceCreated', cloned)
    emit('sequenceSelected', cloned)
    
    ElMessage.success(`Cloned as "${cloned.name}"`)
  } catch (err) {
    console.error('Failed to clone sequence:', err)
    ElMessage.error('Failed to clone sequence')
  } finally {
    closeContextMenu()
  }
}

async function handleDelete() {
  if (!selectedSequence.value) return
  
  const sequence = selectedSequence.value
  
  try {
    await ElMessageBox.confirm(
      `Are you sure you want to delete "${sequence.name}"? This action cannot be undone.`,
      'Delete Sequence',
      {
        confirmButtonText: 'Delete',
        cancelButtonText: 'Cancel',
        type: 'warning',
      }
    )
    
    await deleteSequence(sequence.id)
    
    sequences.value = sequences.value.filter(s => s.id !== sequence.id)
    
    const tab = tabsStore.getTabBySequenceId(sequence.id)
    if (tab) {
      tabsStore.closeTab(tab.id)
    }
    
    ElMessage.success(`Deleted "${sequence.name}"`)
  } catch (err) {
    if (err === 'cancel') return
    
    console.error('Failed to delete sequence:', err)
    ElMessage.error('Failed to delete sequence')
  } finally {
    closeContextMenu()
  }
}

function formatDate(dateString?: string): string {
  if (!dateString) return ''
  
  const date = new Date(dateString)
  const now = new Date()
  const diffMs = now.getTime() - date.getTime()
  const diffMins = Math.floor(diffMs / 60000)
  const diffHours = Math.floor(diffMs / 3600000)
  const diffDays = Math.floor(diffMs / 86400000)
  
  if (diffMins < 1) return 'Just now'
  if (diffMins < 60) return `${diffMins}m ago`
  if (diffHours < 24) return `${diffHours}h ago`
  if (diffDays < 7) return `${diffDays}d ago`
  
  return date.toLocaleDateString()
}
</script>

<template>
  <div class="sequence-list-panel">
    <div class="panel-header">
      <h2 class="panel-title">Sequences</h2>
      <span class="panel-count">{{ filteredCount }} / {{ totalCount }}</span>
    </div>
    
    <div class="search-container">
      <ElInput
        v-model="searchQuery"
        placeholder="Search sequences..."
        :prefix-icon="Search"
        clearable
        size="default"
        class="search-input"
      />
    </div>
    
    <div v-if="isLoading" class="loading-container">
      <div class="spinner" />
      <span class="loading-text">Loading sequences...</span>
    </div>
    
    <ElEmpty v-else-if="error" :description="error" />
    
    <ElEmpty v-else-if="filteredSequences.length === 0" :description="searchQuery ? 'No sequences match your search' : 'No sequences found'" />
    
    <ElScrollbar v-else class="sequence-list">
      <div class="sequences">
        <div
          v-for="sequence in filteredSequences"
          :key="sequence.id"
          class="sequence-item"
          @dblclick="handleDoubleClick(sequence)"
          @contextmenu="handleContextMenu($event, sequence)"
        >
          <div class="sequence-header">
            <span class="sequence-name">{{ sequence.name }}</span>
            <span v-if="sequence.version" class="sequence-version">v{{ sequence.version }}</span>
          </div>
          
          <p v-if="sequence.description" class="sequence-description">
            {{ sequence.description }}
          </p>
          
          <div class="sequence-meta">
            <div v-if="sequence.tags?.length" class="sequence-tags">
              <span
                v-for="tag in sequence.tags.slice(0, 2)"
                :key="tag"
                class="tag"
              >
                {{ tag }}
              </span>
              <span v-if="sequence.tags.length > 2" class="tag-more">
                +{{ sequence.tags.length - 2 }}
              </span>
            </div>
            <span v-if="sequence.updated_at" class="sequence-date">
              {{ formatDate(sequence.updated_at) }}
            </span>
          </div>
        </div>
      </div>
    </ElScrollbar>
    
    <Teleport to="body">
      <div
        v-if="contextMenuVisible"
        class="context-menu"
        :style="{
          left: `${contextMenuPosition.x}px`,
          top: `${contextMenuPosition.y}px`
        }"
      >
        <button class="context-menu-item" @click="handleClone">
          <el-icon class="context-menu-icon"><DocumentCopy /></el-icon>
          <span>Clone</span>
        </button>
        <button v-if="hasScope('flow:write')" class="context-menu-item context-menu-item-danger" @click="handleDelete">
          <el-icon class="context-menu-icon"><Delete /></el-icon>
          <span>Delete</span>
        </button>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.sequence-list-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--color-bg-primary, #fff);
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--spacing-md, 1rem);
  border-bottom: 1px solid var(--color-border-default, #e5e7eb);
}

.panel-title {
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--color-text-primary, #111827);
  margin: 0;
}

.panel-count {
  font-size: 0.75rem;
  color: var(--color-text-tertiary, #9ca3af);
  padding: 0.125rem 0.5rem;
  background: var(--color-bg-secondary, #f9fafb);
  border-radius: var(--radius-sm, 0.25rem);
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
  border-top-color: var(--color-primary, #409eff);
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

.sequence-list {
  flex: 1;
  min-height: 0;
}

.sequences {
  padding: var(--spacing-sm, 0.5rem);
}

.sequence-item {
  padding: var(--spacing-sm, 0.5rem) var(--spacing-md, 1rem);
  margin: var(--spacing-xs, 0.25rem) 0;
  background: var(--color-bg-primary, #fff);
  border: 1px solid var(--color-border-default, #e5e7eb);
  border-radius: var(--radius-lg, 0.5rem);
  cursor: pointer;
  transition: all var(--transition-fast, 150ms ease);
  user-select: none;
}

.sequence-item:hover {
  border-color: var(--color-primary, #409eff);
  box-shadow: 0 2px 4px rgba(139, 92, 246, 0.1);
}

.sequence-item:active {
  transform: scale(0.98);
}

.sequence-header {
  display: flex;
  align-items: baseline;
  gap: var(--spacing-sm, 0.5rem);
}

.sequence-name {
  font-size: 0.875rem;
  font-weight: 500;
  color: var(--color-text-primary, #111827);
}

.sequence-version {
  font-size: 0.6875rem;
  color: var(--color-text-tertiary, #9ca3af);
  padding: 0.125rem 0.375rem;
  background: var(--color-bg-tertiary, #f3f4f6);
  border-radius: var(--radius-sm, 0.25rem);
}

.sequence-description {
  font-size: 0.75rem;
  color: var(--color-text-secondary, #4b5563);
  margin: 0.25rem 0 0;
  line-height: 1.4;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.sequence-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: var(--spacing-xs, 0.25rem);
}

.sequence-tags {
  display: flex;
  align-items: center;
  gap: 0.25rem;
}

.tag {
  font-size: 0.625rem;
  padding: 0.125rem 0.375rem;
  background: var(--color-bg-tertiary, #f3f4f6);
  border-radius: var(--radius-sm, 0.25rem);
  color: var(--color-text-secondary, #4b5563);
}

.tag-more {
  font-size: 0.625rem;
  color: var(--color-text-tertiary, #9ca3af);
  padding: 0.125rem 0.25rem;
}

.sequence-date {
  font-size: 0.625rem;
  color: var(--color-text-tertiary, #9ca3af);
}

.context-menu {
  position: fixed;
  z-index: var(--z-popover, 1060);
  background: var(--color-bg-elevated, #fff);
  border: 1px solid var(--color-border-default, #e5e7eb);
  border-radius: var(--radius-lg, 0.5rem);
  box-shadow: var(--shadow-lg, 0 10px 15px -3px rgba(0, 0, 0, 0.1));
  padding: var(--spacing-xs, 0.25rem);
  min-width: 120px;
}

.context-menu-item {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm, 0.5rem);
  width: 100%;
  padding: var(--spacing-sm, 0.5rem) var(--spacing-md, 1rem);
  background: transparent;
  border: none;
  border-radius: var(--radius-md, 0.375rem);
  font-size: 0.875rem;
  color: var(--color-text-primary, #111827);
  cursor: pointer;
  transition: background-color var(--transition-fast, 150ms ease);
}

.context-menu-item:hover {
  background: var(--color-bg-tertiary, #f3f4f6);
}

.context-menu-item-danger {
  color: var(--color-error, #ef4444);
}

.context-menu-item-danger:hover {
  background: rgba(239, 68, 68, 0.1);
}

.context-menu-icon {
  font-size: 1rem;
}
</style>
