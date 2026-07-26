import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

/**
 * Represents a single tab in the sequence editor
 */
export interface SequenceTab {
  /** Unique identifier for this tab instance */
  id: string
  /** ID of the sequence this tab represents */
  sequenceId: string
  /** Display name shown in the tab */
  name: string
  /** Whether the sequence has unsaved changes */
  isDirty: boolean
}

/**
 * Pinia store for managing sequence editor tabs
 * 
 * Features:
 * - Multiple sequence tabs management
 * - Active tab tracking
 * - Dirty state tracking for unsaved changes
 */
export const useTabsStore = defineStore('tabs', () => {
  // State
  const tabs = ref<SequenceTab[]>([])
  const activeTabId = ref<string | null>(null)

  // Getters
  const activeTab = computed(() => {
    if (!activeTabId.value) return null
    return tabs.value.find(tab => tab.id === activeTabId.value) || null
  })

  const tabCount = computed(() => tabs.value.length)

  const hasDirtyTabs = computed(() => 
    tabs.value.some(tab => tab.isDirty)
  )

  // Actions

  /**
   * Generate a unique tab ID
   */
  function generateTabId(): string {
    return `tab-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`
  }

  /**
   * Add a new tab for a sequence
   * If a tab with the same sequenceId already exists, activate it instead
   */
  function addTab(sequenceId: string, name: string): SequenceTab {
    // Check if tab already exists for this sequence
    const existingTab = tabs.value.find(tab => tab.sequenceId === sequenceId)
    if (existingTab) {
      activeTabId.value = existingTab.id
      return existingTab
    }

    // Create new tab
    const newTab: SequenceTab = {
      id: generateTabId(),
      sequenceId,
      name,
      isDirty: false,
    }

    tabs.value.push(newTab)
    activeTabId.value = newTab.id

    return newTab
  }

  /**
   * Close a tab by its ID
   * If closing the active tab, activate the previous tab (or next if no previous)
   * Returns true if the tab was closed, false if it didn't exist
   */
  function closeTab(tabId: string): boolean {
    const index = tabs.value.findIndex(tab => tab.id === tabId)
    if (index === -1) return false

    // Remove the tab
    tabs.value.splice(index, 1)

    // If we closed the active tab, activate another
    if (activeTabId.value === tabId) {
      if (tabs.value.length === 0) {
        activeTabId.value = null
      } else {
        // Activate previous tab, or next if at the beginning
        const newIndex = Math.min(index, tabs.value.length - 1)
        activeTabId.value = tabs.value[newIndex].id
      }
    }

    return true
  }

  /**
   * Set the active tab by ID
   */
  function setActiveTab(tabId: string): void {
    const tab = tabs.value.find(t => t.id === tabId)
    if (tab) {
      activeTabId.value = tabId
    }
  }

  /**
   * Mark a tab as having unsaved changes
   */
  function markDirty(tabId: string, dirty: boolean = true): void {
    const tab = tabs.value.find(t => t.id === tabId)
    if (tab) {
      tab.isDirty = dirty
    }
  }

  /**
   * Update the display name of a tab
   */
  function updateTabName(tabId: string, name: string): void {
    const tab = tabs.value.find(t => t.id === tabId)
    if (tab) {
      tab.name = name
    }
  }

  /**
   * Get a tab by sequence ID
   */
  function getTabBySequenceId(sequenceId: string): SequenceTab | undefined {
    return tabs.value.find(tab => tab.sequenceId === sequenceId)
  }

  /**
   * Clear all tabs (useful for reset/logout)
   */
  function clearAllTabs(): void {
    tabs.value = []
    activeTabId.value = null
  }

  return {
    // State
    tabs,
    activeTabId,
    
    // Getters
    activeTab,
    tabCount,
    hasDirtyTabs,
    
    // Actions
    addTab,
    closeTab,
    setActiveTab,
    markDirty,
    updateTabName,
    getTabBySequenceId,
    clearAllTabs,
  }
})
