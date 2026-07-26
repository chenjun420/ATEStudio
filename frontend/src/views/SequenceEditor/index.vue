<script setup lang="ts">
import { provide, ref, shallowRef, watch, computed } from 'vue'
import type { Graph, Node } from '@antv/x6'
import GraphContainer from './components/GraphContainer.vue'
import SubGraphContainer from './components/SubGraphContainer.vue'
import BreadcrumbNav from './components/BreadcrumbNav.vue'
import StepLibraryPanel from './components/StepLibraryPanel.vue'
import NodeManagerPanel from './components/NodeManagerPanel.vue'
import PropertyPanel from './components/PropertyPanel.vue'
import Toolbar from './components/Toolbar.vue'
import SequenceTabs from './components/SequenceTabs.vue'
import SequenceListPanel from './components/SequenceListPanel.vue'
import ScriptEditorDialog from '@/components/ScriptEditorDialog.vue'
import { useSerializer } from '@/composables/useSerializer'
import { useTabsStore } from '@/stores/tabs'
import { createSequence, type Sequence } from '@/api/sequences'
import { isLoopContainerData } from '@/models/nodes/types'

// Provide selected node ID for child components
const selectedNodeId = ref<string | null>(null)
provide('selectedNodeId', selectedNodeId)

// Provide graph instance for property panel to access node data
const graphInstance = shallowRef<Graph | null>(null)
provide('graphInstance', graphInstance)

// Current sequence being edited
const currentSequence = ref<Sequence | null>(null)
provide('currentSequence', currentSequence)

// Current execution run ID (for SSE status updates)
const currentRunId = ref('')

// Serializer for loading sequences
const { importYamlToGraph } = useSerializer()

// Tabs store
const tabsStore = useTabsStore()

// ============================================
// Sub-graph navigation state
// ============================================

/** The ID of the loop container node currently being viewed in sub-graph, or null for main graph */
const activeSubGraphContainerId = ref<string | null>(null)

/** Ref to the SubGraphContainer component for calling syncBackToMainGraph */
const subGraphRef = ref<InstanceType<typeof SubGraphContainer> | null>(null)

// ============================================
// Script Editor Dialog state
// ============================================

/** Whether the script editor dialog is visible */
const scriptEditorVisible = ref(false)

/** The ID of the script being edited */
const scriptEditorScriptId = ref('')

/** The name of the script being edited */
const scriptEditorScriptName = ref('')

/**
 * Handle "edit-script" event from PropertyPanel or GraphContainer
 */
function handleEditScript(payload: { scriptId: string; scriptName: string }) {
  scriptEditorScriptId.value = payload.scriptId
  scriptEditorScriptName.value = payload.scriptName
  scriptEditorVisible.value = true
}

/**
 * Handle script editor dialog close
 */
function handleScriptEditorClose() {
  scriptEditorVisible.value = false
}

/**
 * Handle script saved from editor dialog
 */
function handleScriptSaved() {
  // Script was saved — could refresh preview in PropertyPanel if needed
  console.log('Script saved:', scriptEditorScriptName.value)
}

/** Breadcrumb items for navigation */
const breadcrumbItems = computed(() => {
  const items = [{ id: '__main__', label: '主图' }]

  if (activeSubGraphContainerId.value && graphInstance.value) {
    const cell = graphInstance.value.getCellById(activeSubGraphContainerId.value)
    if (cell && cell.isNode()) {
      const node = cell as Node
      const data = node.getData()
      // Build label from loop container data
      let label = '循环容器'
      if (isLoopContainerData(data)) {
        const loopTypeMap: Record<string, string> = { for: 'For', while: 'While', foreach: 'ForEach' }
        label = `${loopTypeMap[data.loopType] || data.loopType} Loop`
        if (data.loopId) {
          label += ` ${data.loopId.slice(0, 8)}`
        }
      }
      items.push({ id: activeSubGraphContainerId.value, label })
    }
  }

  return items
})

/** Whether we are currently in sub-graph view */
const isInSubGraph = computed(() => activeSubGraphContainerId.value !== null)

/**
 * Handle entering sub-graph view when a loop container is double-clicked
 */
function handleEnterSubGraph(containerNodeId: string) {
  activeSubGraphContainerId.value = containerNodeId
}

/**
 * Handle breadcrumb navigation — return to main graph
 */
function handleBreadcrumbNavigate(id: string) {
  if (id === '__main__') {
    exitSubGraph()
  }
}

/**
 * Exit sub-graph view and return to main graph.
 * The SubGraphContainer's onUnmounted hook will sync changes back.
 */
function exitSubGraph() {
  activeSubGraphContainerId.value = null
}

// Watch for graph initialization and load pending sequence
watch(graphInstance, (graph) => {
  if (graph && currentSequence.value?.yaml_content) {
    try {
      importYamlToGraph(graph, currentSequence.value.yaml_content, true)
    } catch (error) {
      console.error('Failed to load sequence into graph:', error)
    }
  }
})

// Handle sequence selection from toolbar
function handleSequenceSelected(sequence: Sequence) {
  currentSequence.value = sequence
  
  // Load sequence into graph if graph is ready and has content
  if (graphInstance.value && sequence.yaml_content) {
    try {
      importYamlToGraph(graphInstance.value, sequence.yaml_content, true)
    } catch (error) {
      console.error('Failed to load sequence into graph:', error)
    }
  }
}

// Handle new sequence creation
function handleSequenceCreated(sequence: Sequence) {
  currentSequence.value = sequence
  // Clear graph for new sequence
  if (graphInstance.value) {
    graphInstance.value.clearCells()
  }
}

// Left sidebar tab state
const leftSidebarTab = ref<'library' | 'nodes' | 'sequences'>('library')
const activeLeftTab = computed({
  get: () => leftSidebarTab.value,
  set: (val) => { leftSidebarTab.value = val }
})

// Handle tab selection
function handleTabSelect(tabId: string) {
  tabsStore.setActiveTab(tabId)
  const tab = tabsStore.tabs.find(t => t.id === tabId)
  if (tab) {
    const sequence = { id: tab.sequenceId, name: tab.name } as Sequence
    currentSequence.value = sequence
  }
}

// Handle tab close
function handleTabClose(tabId: string) {
  tabsStore.closeTab(tabId)
}

// Handle new sequence creation from tabs
async function handleTabNew() {
  const timestamp = new Date().toISOString().slice(0, 19).replace('T', ' ')
  const defaultSequence = {
    name: `New Sequence ${tabsStore.tabCount + 1}`,
    description: 'A new test sequence',
    version: '1.0.0',
    yaml_content: 'steps:\n  []\n',
  }
  
  try {
    const newSequence = await createSequence(defaultSequence)
    tabsStore.addTab(newSequence.id, newSequence.name)
    currentSequence.value = newSequence
    
    if (graphInstance.value) {
      graphInstance.value.clearCells()
    }
  } catch (error) {
    console.error('Error creating sequence:', error)
  }
}

// Handle execution started from Toolbar
function handleExecutionStarted(runId: string) {
  currentRunId.value = runId
}

// Handle execution ended from Toolbar
function handleExecutionEnded() {
  // Keep runId for a moment so user can see final status
  // It will be cleared when a new execution starts or sequence changes
}
</script>

<template>
  <div style="display: flex; height: 100vh; background-color: #fafafa;">
    <!-- Left sidebar: Step Library + Node Manager + Sequence List -->
    <aside style="width: 256px; border-right: 1px solid #e5e7eb; background: white; display: flex; flex-direction: column;">
      <!-- Tab header -->
      <div style="display: flex; border-bottom: 1px solid #e5e7eb;">
        <button
          class="sidebar-tab"
          :class="{ active: leftSidebarTab === 'library' }"
          @click="leftSidebarTab = 'library'"
        >
          Scripts
        </button>
        <button
          class="sidebar-tab"
          :class="{ active: leftSidebarTab === 'nodes' }"
          @click="leftSidebarTab = 'nodes'"
        >
          Nodes
        </button>
        <button
          class="sidebar-tab"
          :class="{ active: leftSidebarTab === 'sequences' }"
          @click="leftSidebarTab = 'sequences'"
        >
          Sequences
        </button>
      </div>

      <!-- Tab content -->
      <div style="flex: 1; overflow: hidden;">
        <StepLibraryPanel v-show="leftSidebarTab === 'library'" />
        <NodeManagerPanel v-show="leftSidebarTab === 'nodes'" />
        <SequenceListPanel 
          v-show="leftSidebarTab === 'sequences'"
          @sequence-selected="handleSequenceSelected"
          @sequence-created="handleSequenceCreated"
        />
      </div>
    </aside>

    <!-- Main content: Canvas area -->
    <main style="flex: 1; display: flex; flex-direction: column;">
      <!-- Tabs bar -->
      <SequenceTabs
        :tabs="tabsStore.tabs"
        :active-id="tabsStore.activeTabId"
        @select="handleTabSelect"
        @close="handleTabClose"
        @new="handleTabNew"
      />
      
      <!-- Toolbar header -->
      <header style="height: 48px; border-bottom: 1px solid #e5e7eb; background: white; display: flex; align-items: center; padding: 0 16px;">
        <Toolbar @sequence-selected="handleSequenceSelected" @sequence-created="handleSequenceCreated" @execution-started="handleExecutionStarted" @execution-ended="handleExecutionEnded" />
      </header>

      <!-- Graph canvas container -->
      <div style="flex: 1; position: relative; display: flex; flex-direction: column;">
        <!-- Breadcrumb navigation (shown when in sub-graph view) -->
        <BreadcrumbNav
          :items="breadcrumbItems"
          @navigate="handleBreadcrumbNavigate"
        />

        <!-- Main graph (shown when not in sub-graph view) -->
        <GraphContainer
          v-show="!isInSubGraph"
          :run-id="currentRunId"
          @enter-sub-graph="handleEnterSubGraph"
          @edit-script="handleEditScript"
        />

        <!-- Sub-graph (shown when viewing a loop container's children) -->
        <SubGraphContainer
          v-if="isInSubGraph"
          ref="subGraphRef"
          :container-node-id="activeSubGraphContainerId!"
        />
      </div>
    </main>

    <!-- Right sidebar: Property Panel -->
    <aside style="width: 320px; border-left: 1px solid #e5e7eb; background: white;">
      <PropertyPanel @edit-script="handleEditScript" />
    </aside>

    <!-- Script Editor Dialog -->
    <ScriptEditorDialog
      v-model:visible="scriptEditorVisible"
      :script-id="scriptEditorScriptId"
      :script-name="scriptEditorScriptName"
      @saved="handleScriptSaved"
    />
  </div>
</template>

<style scoped>
/* Sidebar tab styles */
.sidebar-tab {
  flex: 1;
  padding: 0.75rem 1rem;
  font-size: 0.75rem;
  font-weight: 500;
  color: var(--color-text-secondary, #4b5563);
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  cursor: pointer;
  transition: all 150ms ease;
}

.sidebar-tab:hover {
  color: var(--color-text-primary, #111827);
  background: var(--color-bg-secondary, #f9fafb);
}

.sidebar-tab.active {
  color: var(--color-primary, #8b5cf6);
  border-bottom-color: var(--color-primary, #8b5cf6);
}
</style>