<script setup lang="ts">
import { inject, ref, shallowRef, computed, onMounted, onUnmounted, watch } from 'vue'
import type { Ref, ShallowRef } from 'vue'
import type { Graph, Node } from '@antv/x6'
import { RecycleScroller } from 'vue-virtual-scroller'
import { ElInput, ElEmpty, ElButton, ElDropdown, ElDropdownMenu, ElDropdownItem, ElDialog, ElIcon } from 'element-plus'
import { Search, Plus, Folder, FolderOpened, MoreFilled, Edit, Delete } from '@element-plus/icons-vue'
import { v4 as uuidv4 } from 'uuid'
import { isScriptStepData, isVariableData, type NodeGroup } from '@/models/nodes/types'
import 'vue-virtual-scroller/dist/vue-virtual-scroller.css'

// Get selected node ID and graph instance from parent.
// Default refs keep the panel defined when mounted standalone.
const selectedNodeId = inject<Ref<string | null>>('selectedNodeId', ref<string | null>(null))
const graphInstance = inject<ShallowRef<Graph | null>>('graphInstance', shallowRef<Graph | null>(null))

// ============================================
// Node List Management
// ============================================

// Reactive node list
const nodes = ref<Array<{
  id: string
  name: string
  type: string
  data: unknown
  groupId?: string
}>>([])

// Update node list from graph
function updateNodeList() {
  if (!graphInstance?.value) {
    nodes.value = []
    return
  }

  const graphNodes = graphInstance.value.getNodes()
  nodes.value = graphNodes.map(node => {
    const data = node.getData()
    let name = node.id.slice(0, 8) // Default to truncated ID
    let groupId: string | undefined

    // Extract display name from node data
    if (isScriptStepData(data)) {
      name = data.stepId || data.scriptName || node.id.slice(0, 8)
      groupId = data.groupId
    } else if (isVariableData(data)) {
      const varKeys = Object.keys(data.variables)
      name = varKeys.length > 0 ? varKeys[0] : 'Variables'
      groupId = data.groupId
    }

    return {
      id: node.id,
      name,
      type: node.shape || 'unknown',
      data,
      groupId,
    }
  })
}

// Get display name for node type
function getNodeTypeName(type: string): string {
  const typeNames: Record<string, string> = {
    'script-step-node': 'Script Step',
    'variable-node': 'Variables',
  }
  return typeNames[type] || type.replace(/-/g, ' ')
}

// Get type icon for node
function getNodeTypeIcon(type: string): string {
  const icons: Record<string, string> = {
    'script-step-node': '⚡',
    'variable-node': '📦',
  }
  return icons[type] || '📄'
}

// Handle node click - select in graph
function handleNodeClick(nodeId: string) {
  if (!graphInstance?.value) return

  const node = graphInstance.value.getCellById(nodeId) as Node | null
  if (node) {
    graphInstance.value.resetSelection([node])
    if (selectedNodeId) {
      selectedNodeId.value = nodeId
    }
  }
}

// Check if node is selected
function isSelected(nodeId: string): boolean {
  return selectedNodeId?.value === nodeId
}

// ============================================
// Search & Filter (Task 6)
// ============================================

const searchQuery = ref('')

// Filter nodes by search query
const filteredNodes = computed(() => {
  if (!searchQuery.value.trim()) return nodes.value

  const query = searchQuery.value.toLowerCase()
  return nodes.value.filter(node => {
    // Search by node ID
    if (node.id.toLowerCase().includes(query)) return true

    // Search by name (stepId/scriptName)
    if (node.name.toLowerCase().includes(query)) return true

    // Search by type
    if (getNodeTypeName(node.type).toLowerCase().includes(query)) return true

    return false
  })
})

// Check if search has no results
const hasNoResults = computed(() => {
  return searchQuery.value.trim() && filteredNodes.value.length === 0
})

// ============================================
// Group Management (Task 7)
// ============================================

// Groups state
const groups = ref<NodeGroup[]>([])
const collapsedGroups = ref<Set<string>>(new Set())

// Group dialog state
const showGroupDialog = ref(false)
const editingGroup = ref<NodeGroup | null>(null)
const newGroupName = ref('')

// Group color options
const groupColors = [
  '#409eff', // Primary blue
  '#3b82f6', // Blue
  '#10b981', // Green
  '#f59e0b', // Orange
  '#ef4444', // Red
  '#ec4899', // Pink
  '#06b6d4', // Cyan
]

// Get nodes grouped
const groupedNodes = computed(() => {
  const grouped: Map<string | null, typeof nodes.value> = new Map()
  grouped.set(null, []) // Ungrouped nodes

  filteredNodes.value.forEach(node => {
    if (node.groupId) {
      if (!grouped.has(node.groupId)) {
        grouped.set(node.groupId, [])
      }
      grouped.get(node.groupId)!.push(node)
    } else {
      grouped.get(null)!.push(node)
    }
  })

  return grouped
})

// Grouped view with group headers
const listItems = computed(() => {
  const items: Array<{ type: 'group' | 'node'; data: unknown }> = []

  groups.value.forEach(group => {
    const groupNodes = groupedNodes.value.get(group.id) || []
    if (groupNodes.length > 0 || !searchQuery.value.trim()) {
      items.push({ type: 'group', data: group })
      if (!collapsedGroups.value.has(group.id)) {
        groupNodes.forEach(node => {
          items.push({ type: 'node', data: node })
        })
      }
    }
  })

  // Ungrouped nodes
  const ungrouped = groupedNodes.value.get(null) || []
  ungrouped.forEach(node => {
    items.push({ type: 'node', data: node })
  })

  return items
})

// Create new group
function createGroup() {
  editingGroup.value = null
  newGroupName.value = ''
  showGroupDialog.value = true
}

// Edit existing group
function editGroup(group: NodeGroup) {
  editingGroup.value = { ...group }
  newGroupName.value = group.name
  showGroupDialog.value = true
}

// Update the color of the group currently being edited (color picker in dialog)
function setEditingGroupColor(color: string) {
  if (editingGroup.value) {
    editingGroup.value = { ...editingGroup.value, color }
  }
}

// Save group (create or update)
function saveGroup() {
  if (!newGroupName.value.trim()) return

  if (editingGroup.value) {
    // Update existing group
    const index = groups.value.findIndex(g => g.id === editingGroup.value!.id)
    if (index !== -1) {
      groups.value[index] = {
        ...editingGroup.value,
        name: newGroupName.value.trim(),
      }
    }
  } else {
    // Create new group
    const newGroup: NodeGroup = {
      id: uuidv4(),
      name: newGroupName.value.trim(),
      color: groupColors[groups.value.length % groupColors.length],
      collapsed: false,
    }
    groups.value.push(newGroup)
  }

  showGroupDialog.value = false
  saveGroupsToStorage()
}

// Delete group
function deleteGroup(group: NodeGroup) {
  const index = groups.value.findIndex(g => g.id === group.id)
  if (index !== -1) {
    groups.value.splice(index, 1)

    // Remove groupId from nodes in this group
    const groupNodes = groupedNodes.value.get(group.id) || []
    groupNodes.forEach(nodeItem => {
      updateNodeGroupId(nodeItem.id, undefined)
    })

    saveGroupsToStorage()
  }
}

// Toggle group collapse
function toggleGroupCollapse(groupId: string) {
  if (collapsedGroups.value.has(groupId)) {
    collapsedGroups.value.delete(groupId)
  } else {
    collapsedGroups.value.add(groupId)
  }
}

// Move node to group
function moveNodeToGroup(nodeId: string, groupId: string | undefined) {
  updateNodeGroupId(nodeId, groupId)
}

// Update node's groupId in graph data
function updateNodeGroupId(nodeId: string, groupId: string | undefined) {
  if (!graphInstance?.value) return

  const node = graphInstance.value.getCellById(nodeId) as Node | null
  if (!node) return

  const data = node.getData()
  if (isScriptStepData(data)) {
    node.setData({ ...data, groupId })
  } else if (isVariableData(data)) {
    node.setData({ ...data, groupId })
  }

  updateNodeList()
}

// Check if node is being dragged over a group
const draggedNodeId = ref<string | null>(null)

// Drag handlers for nodes
function onNodeDragStart(event: DragEvent, nodeId: string) {
  if (!event.dataTransfer) return

  draggedNodeId.value = nodeId
  event.dataTransfer.setData('text/plain', nodeId)
  event.dataTransfer.effectAllowed = 'move'

  const target = event.target as HTMLElement
  target.style.opacity = '0.6'
}

function onNodeDragEnd(event: DragEvent) {
  const target = event.target as HTMLElement
  target.style.opacity = '1'
  draggedNodeId.value = null
}

// Drop handler for group
function onGroupDrop(event: DragEvent, groupId: string) {
  event.preventDefault()
  const nodeId = event.dataTransfer?.getData('text/plain')
  if (nodeId) {
    moveNodeToGroup(nodeId, groupId)
  }
}

function onGroupDragOver(event: DragEvent) {
  event.preventDefault()
  event.dataTransfer!.dropEffect = 'move'
}

// Storage for groups
const GROUPS_STORAGE_KEY = 'ate-studio-node-groups'

function saveGroupsToStorage() {
  localStorage.setItem(GROUPS_STORAGE_KEY, JSON.stringify(groups.value))
}

function loadGroupsFromStorage() {
  const stored = localStorage.getItem(GROUPS_STORAGE_KEY)
  if (stored) {
    try {
      groups.value = JSON.parse(stored)
    } catch {
      groups.value = []
    }
  }
}

// ============================================
// Virtual Scroller Configuration (Task 5)
// ============================================

const ITEM_SIZE = 56 // Height of each node item
const GROUP_HEADER_SIZE = 40 // Height of group header

// Calculate item size based on type
function getItemSize(index: number): number {
  const item = listItems.value[index]
  if (item?.type === 'group') {
    return GROUP_HEADER_SIZE
  }
  return ITEM_SIZE
}

// ============================================
// Lifecycle & Event Handlers
// ============================================

// Node change handlers
function handleNodeAdded() {
  updateNodeList()
}

function handleNodeRemoved() {
  updateNodeList()
}

function handleNodeChanged() {
  updateNodeList()
}

// Watch graph instance and setup listeners
watch(graphInstance, (graph, _, onCleanup) => {
  if (graph) {
    // Initial update
    updateNodeList()

    // Setup event listeners
    graph.on('node:added', handleNodeAdded)
    graph.on('node:removed', handleNodeRemoved)
    graph.on('change:data', handleNodeChanged)

    // Cleanup on unwatch
    onCleanup(() => {
      graph.off('node:added', handleNodeAdded)
      graph.off('node:removed', handleNodeRemoved)
      graph.off('change:data', handleNodeChanged)
    })
  }
}, { immediate: true })

// Load groups on mount
onMounted(() => {
  loadGroupsFromStorage()
})

// Cleanup on unmount
onUnmounted(() => {
  if (graphInstance?.value) {
    graphInstance.value.off('node:added', handleNodeAdded)
    graphInstance.value.off('node:removed', handleNodeRemoved)
    graphInstance.value.off('change:data', handleNodeChanged)
  }
})
</script>

<template>
  <div class="node-manager">
    <!-- Panel header -->
    <div class="panel-header">
      <div class="header-left">
        <h2 class="panel-title">Nodes</h2>
        <span class="panel-count">{{ nodes.length }} total</span>
      </div>
      <ElButton
        size="small"
        :icon="Plus"
        @click="createGroup"
        title="Create group"
      >
        Group
      </ElButton>
    </div>

    <!-- Search input (Task 6) -->
    <div class="search-container">
      <ElInput
        v-model="searchQuery"
        placeholder="Search by ID, name, or type..."
        :prefix-icon="Search"
        clearable
        size="default"
        class="search-input"
      />
    </div>

    <!-- Node list with virtual scrolling (Task 5) -->
    <div class="node-list">
      <!-- Empty state -->
      <ElEmpty
        v-if="nodes.length === 0"
        description="No nodes on canvas"
      >
        <template #image>
          <div class="empty-icon">📭</div>
        </template>
        <p class="empty-hint">Drag scripts from the library to create nodes</p>
      </ElEmpty>

      <!-- No search results -->
      <ElEmpty
        v-else-if="hasNoResults"
        description="No matching nodes found"
      >
        <template #image>
          <div class="empty-icon">🔍</div>
        </template>
        <p class="empty-hint">Try a different search term</p>
      </ElEmpty>

      <!-- Virtual list -->
      <RecycleScroller
        v-else
        class="virtual-scroller"
        :items="listItems"
        :item-size="ITEM_SIZE"
        :buffer="200"
        key-field="data.id"
        :estimate-size="getItemSize"
      >
        <template #default="{ item }">
          <!-- Group header -->
          <div
            v-if="item.type === 'group'"
            class="group-header"
            :style="{ borderLeftColor: (item.data as NodeGroup).color }"
            @dragover="onGroupDragOver"
            @drop="onGroupDrop($event, (item.data as NodeGroup).id)"
          >
            <button
              class="group-toggle"
              @click="toggleGroupCollapse((item.data as NodeGroup).id)"
            >
              <ElIcon :size="14">
                <FolderOpened v-if="!collapsedGroups.has((item.data as NodeGroup).id)" />
                <Folder v-else />
              </ElIcon>
              <span class="group-name">{{ (item.data as NodeGroup).name }}</span>
              <span class="group-count">{{ groupedNodes.get((item.data as NodeGroup).id)?.length || 0 }}</span>
            </button>
            <ElDropdown trigger="click" @command="(cmd: string) => cmd === 'edit' ? editGroup(item.data as NodeGroup) : deleteGroup(item.data as NodeGroup)">
              <button class="group-actions" @click.stop>
                <ElIcon :size="14"><MoreFilled /></ElIcon>
              </button>
              <template #dropdown>
                <ElDropdownMenu>
                  <ElDropdownItem command="edit">
                    <ElIcon><Edit /></ElIcon>
                    Rename
                  </ElDropdownItem>
                  <ElDropdownItem command="delete" divided>
                    <ElIcon><Delete /></ElIcon>
                    Delete
                  </ElDropdownItem>
                </ElDropdownMenu>
              </template>
            </ElDropdown>
          </div>

          <!-- Node item -->
          <button
            v-else
            class="node-item"
            :class="{ selected: isSelected((item.data as { id: string }).id) }"
            draggable="true"
            @click="handleNodeClick((item.data as { id: string }).id)"
            @dragstart="onNodeDragStart($event, (item.data as { id: string }).id)"
            @dragend="onNodeDragEnd"
          >
            <div class="node-icon">{{ getNodeTypeIcon((item.data as { type: string }).type) }}</div>
            <div class="node-info">
              <span class="node-name">{{ (item.data as { name: string }).name }}</span>
              <span class="node-type">{{ getNodeTypeName((item.data as { type: string }).type) }}</span>
            </div>
            <span class="node-id">{{ (item.data as { id: string }).id.slice(0, 8) }}</span>
          </button>
        </template>
      </RecycleScroller>
    </div>

    <!-- Group Create/Edit Dialog -->
    <ElDialog
      v-model="showGroupDialog"
      :title="editingGroup ? 'Edit Group' : 'Create Group'"
      width="320px"
      :close-on-click-modal="false"
    >
      <div class="dialog-content">
        <label class="dialog-label">Group Name</label>
        <ElInput
          v-model="newGroupName"
          placeholder="Enter group name"
          @keyup.enter="saveGroup"
        />
        <div v-if="!editingGroup" class="color-picker">
          <label class="dialog-label">Color</label>
          <div class="color-options">
            <button
              v-for="color in groupColors"
              :key="color"
              class="color-option"
              :style="{ backgroundColor: color }"
              @click="setEditingGroupColor(color)"
            />
          </div>
        </div>
      </div>
      <template #footer>
        <ElButton @click="showGroupDialog = false">Cancel</ElButton>
        <ElButton type="primary" @click="saveGroup" :disabled="!newGroupName.trim()">
          {{ editingGroup ? 'Save' : 'Create' }}
        </ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.node-manager {
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

.header-left {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm, 0.5rem);
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
  background: var(--color-bg-secondary, #f3f4f6);
  padding: 0.125rem 0.5rem;
  border-radius: var(--radius-full, 9999px);
}

.search-container {
  padding: var(--spacing-sm, 0.5rem) var(--spacing-md, 1rem);
  border-bottom: 1px solid var(--color-border-default, #e5e7eb);
}

.search-input {
  width: 100%;
}

.search-input :deep(.el-input__wrapper) {
  box-shadow: 0 0 0 1px var(--color-border-default) inset;
  border-radius: var(--radius-md);
}

.search-input :deep(.el-input__wrapper:hover) {
  box-shadow: 0 0 0 1px var(--color-primary) inset;
}

.search-input :deep(.el-input__wrapper.is-focus) {
  box-shadow: 0 0 0 1px var(--color-primary) inset;
}

.node-list {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.virtual-scroller {
  height: 100%;
}

.empty-icon {
  font-size: 2.5rem;
  opacity: 0.5;
  margin-bottom: var(--spacing-md, 1rem);
}

.empty-hint {
  font-size: 0.75rem;
  color: var(--color-text-tertiary, #9ca3af);
  margin: var(--spacing-xs, 0.25rem) 0 0;
}

/* Group Header Styles */
.group-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 40px;
  padding: 0 var(--spacing-md, 1rem);
  margin: var(--spacing-xs, 0.25rem) 0;
  background: var(--color-bg-secondary, #f9fafb);
  border-left: 3px solid var(--color-primary, #409eff);
  border-radius: var(--radius-md, 0.375rem);
  cursor: pointer;
  transition: background-color var(--transition-fast, 150ms ease);
}

.group-header:hover {
  background: var(--color-bg-tertiary, #f3f4f6);
}

.group-toggle {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm, 0.5rem);
  flex: 1;
  background: none;
  border: none;
  cursor: pointer;
  text-align: left;
  padding: 0;
  color: inherit;
}

.group-name {
  font-size: 0.8125rem;
  font-weight: 500;
  color: var(--color-text-primary, #111827);
}

.group-count {
  font-size: 0.6875rem;
  color: var(--color-text-tertiary, #9ca3af);
  background: var(--color-bg-primary, #fff);
  padding: 0.125rem 0.375rem;
  border-radius: var(--radius-sm, 0.25rem);
}

.group-actions {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  background: none;
  border: none;
  border-radius: var(--radius-sm, 0.25rem);
  cursor: pointer;
  color: var(--color-text-tertiary, #9ca3af);
  transition: all var(--transition-fast, 150ms ease);
}

.group-actions:hover {
  background: var(--color-bg-primary, #fff);
  color: var(--color-text-secondary, #4b5563);
}

/* Node Item Styles */
.node-item {
  display: flex;
  align-items: center;
  width: 100%;
  height: 56px;
  padding: var(--spacing-sm, 0.5rem) var(--spacing-md, 1rem);
  margin-bottom: var(--spacing-xs, 0.25rem);
  background: var(--color-bg-primary, #fff);
  border: 1px solid var(--color-border-default, #e5e7eb);
  border-radius: var(--radius-md, 0.375rem);
  cursor: pointer;
  text-align: left;
  transition: all var(--transition-fast, 150ms ease);
}

.node-item:hover {
  background: var(--color-bg-secondary, #f9fafb);
  border-color: var(--color-primary, #409eff);
}

.node-item.selected {
  background: var(--color-primary-50, #ede9fe);
  border-color: var(--color-primary, #409eff);
}

.node-item:active {
  cursor: grabbing;
}

.node-icon {
  font-size: 1rem;
  margin-right: var(--spacing-sm, 0.5rem);
  width: 1.5rem;
  height: 1.5rem;
  display: flex;
  align-items: center;
  justify-content: center;
}

.node-info {
  flex: 1;
  min-width: 0;
}

.node-name {
  display: block;
  font-size: 0.8125rem;
  font-weight: 500;
  color: var(--color-text-primary, #111827);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.node-type {
  display: block;
  font-size: 0.6875rem;
  color: var(--color-text-tertiary, #9ca3af);
}

.node-id {
  font-size: 0.6875rem;
  font-family: var(--font-mono, ui-monospace, monospace);
  color: var(--color-text-tertiary, #9ca3af);
  background: var(--color-bg-tertiary, #f3f4f6);
  padding: 0.125rem 0.375rem;
  border-radius: var(--radius-sm, 0.25rem);
}

/* Dialog Styles */
.dialog-content {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md, 1rem);
}

.dialog-label {
  font-size: 0.875rem;
  font-weight: 500;
  color: var(--color-text-primary, #111827);
}

.color-picker {
  margin-top: var(--spacing-sm, 0.5rem);
}

.color-options {
  display: flex;
  gap: var(--spacing-sm, 0.5rem);
  margin-top: var(--spacing-xs, 0.25rem);
}

.color-option {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  border: 2px solid transparent;
  cursor: pointer;
  transition: transform var(--transition-fast, 150ms ease);
}

.color-option:hover {
  transform: scale(1.1);
}

.color-option:focus {
  outline: none;
  border-color: var(--color-text-primary, #111827);
}

/* Element Plus Dropdown Override */
:deep(.el-dropdown-menu__item) {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm, 0.5rem);
}
</style>
