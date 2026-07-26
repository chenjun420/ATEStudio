<script setup lang="ts">
/**
 * NodeQuickEdit - Floating quick edit form for node properties
 * 
 * Features:
 * - Positioned near the clicked node
 * - Quick edit of stepId and key parameters
 * - Closes on click outside or Escape key
 * - Syncs data back to the node immediately
 */

import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import type { Node } from '@antv/x6'
import { isScriptStepData, type ScriptStepData } from '@/models/nodes/types'

interface Props {
  /** The node being edited */
  node: Node | null
  /** Position for the form (screen coordinates) */
  position: { x: number; y: number } | null
  /** Whether the form is visible */
  visible: boolean
}

const props = defineProps<Props>()
const emit = defineEmits<{
  close: []
  update: [data: ScriptStepData]
}>()

// Local edit state
const editData = ref<ScriptStepData | null>(null)

// Parameter quick edit
const paramKey = ref('')
const paramValue = ref('')

// Compute if editing is for a script step node
const isScriptStep = computed(() => {
  if (!props.node) return false
  const data = props.node.getData()
  return isScriptStepData(data)
})

// Watch node changes to initialize local data
watch(() => props.node, (newNode) => {
  if (newNode && isScriptStepData(newNode.getData())) {
    const data = newNode.getData() as ScriptStepData
    editData.value = { ...data, params: { ...data.params } }
  } else {
    editData.value = null
  }
}, { immediate: true })

// Computed position styles
const positionStyles = computed(() => {
  if (!props.position) return {}
  return {
    left: `${props.position.x}px`,
    top: `${props.position.y}px`,
  }
})

// Handle key parameters for quick access
const keyParameters = computed(() => {
  if (!editData.value?.params) return []
  // Show first 3 parameters for quick editing
  return Object.entries(editData.value.params).slice(0, 3)
})

// Add new parameter
function addParameter() {
  if (!paramKey.value.trim() || !editData.value) return
  
  if (!editData.value.params) {
    editData.value.params = {}
  }
  editData.value.params[paramKey.value.trim()] = paramValue.value || ''
  paramKey.value = ''
  paramValue.value = ''
}

// Remove parameter
function removeParameter(key: string) {
  if (!editData.value?.params) return
  delete editData.value.params[key]
}

// Submit form
function handleSubmit() {
  if (!editData.value) return
  emit('update', editData.value)
  emit('close')
}

// Cancel editing
function handleCancel() {
  emit('close')
}

// Close on Escape key
function handleKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && props.visible) {
    handleCancel()
  }
}

// Setup event listeners
onMounted(() => {
  document.addEventListener('keydown', handleKeydown)
})

onUnmounted(() => {
  document.removeEventListener('keydown', handleKeydown)
})
</script>

<template>
  <Teleport to="body">
    <Transition name="fade">
      <div
        v-if="visible && isScriptStep && editData"
        class="quick-edit-overlay"
        @click.self="handleCancel"
      >
        <div
          class="quick-edit-form"
          :style="positionStyles"
          @click.stop
        >
          <!-- Header -->
          <div class="quick-edit-header">
            <h3 class="quick-edit-title">Quick Edit</h3>
            <button
              class="quick-edit-close"
              @click="handleCancel"
              aria-label="Close"
            >
              <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>

          <!-- Form Content -->
          <form @submit.prevent="handleSubmit" class="quick-edit-content">
            <!-- Step ID -->
            <div class="form-field">
              <label class="form-label">Step ID</label>
              <input
                v-model="editData.stepId"
                type="text"
                class="form-input"
                placeholder="Enter step ID"
              />
            </div>

            <!-- Script Name (read-only) -->
            <div class="form-field">
              <label class="form-label">Script Name</label>
              <input
                v-model="editData.scriptName"
                type="text"
                class="form-input form-input-readonly"
                readonly
              />
            </div>

            <!-- Key Parameters -->
            <div class="form-field" v-if="keyParameters.length > 0">
              <label class="form-label">Parameters</label>
              <div class="param-list">
                <div
                  v-for="[key, value] in keyParameters"
                  :key="key"
                  class="param-item"
                >
                  <span class="param-key">{{ key }}</span>
                  <input
                    :value="String(value)"
                    @change="(e) => editData!.params[key] = (e.target as HTMLInputElement).value"
                    type="text"
                    class="param-input"
                  />
                  <button
                    type="button"
                    class="param-remove"
                    @click="removeParameter(key)"
                    title="Remove parameter"
                  >
                    <svg class="tw-w-3 tw-h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                    </svg>
                  </button>
                </div>
              </div>
            </div>

            <!-- Add Parameter -->
            <div class="add-param">
              <input
                v-model="paramKey"
                type="text"
                class="form-input form-input-sm"
                placeholder="Param key"
              />
              <input
                v-model="paramValue"
                type="text"
                class="form-input form-input-sm"
                placeholder="Param value"
              />
              <button
                type="button"
                class="btn-add-param"
                :disabled="!paramKey.trim()"
                @click="addParameter"
              >
                <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
                </svg>
              </button>
            </div>

            <!-- Actions -->
            <div class="quick-edit-actions">
              <button type="button" class="btn btn-secondary" @click="handleCancel">
                Cancel
              </button>
              <button type="submit" class="btn btn-primary">
                Apply
              </button>
            </div>
          </form>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.quick-edit-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: var(--z-modal);
  background-color: rgba(0, 0, 0, 0.1);
  display: flex;
  align-items: flex-start;
  justify-content: flex-start;
}

.quick-edit-form {
  position: absolute;
  width: 320px;
  background-color: var(--color-bg-elevated);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}

.quick-edit-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--spacing-md) var(--spacing-lg);
  background-color: var(--color-bg-secondary);
  border-bottom: 1px solid var(--color-border-default);
}

.quick-edit-title {
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--color-text-primary);
  margin: 0;
}

.quick-edit-close {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border: none;
  background: transparent;
  color: var(--color-text-tertiary);
  cursor: pointer;
  border-radius: var(--radius-md);
  transition: all var(--transition-fast);
}

.quick-edit-close:hover {
  background-color: var(--color-bg-tertiary);
  color: var(--color-text-primary);
}

.quick-edit-content {
  padding: var(--spacing-lg);
}

.form-field {
  margin-bottom: var(--spacing-md);
}

.form-label {
  display: block;
  font-size: 0.75rem;
  font-weight: 500;
  color: var(--color-text-secondary);
  margin-bottom: var(--spacing-xs);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.form-input {
  width: 100%;
  padding: var(--spacing-sm) var(--spacing-md);
  font-size: 0.875rem;
  color: var(--color-text-primary);
  background-color: var(--color-bg-primary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-md);
  transition: border-color var(--transition-fast);
}

.form-input:focus {
  outline: none;
  border-color: var(--color-primary);
}

.form-input-readonly {
  background-color: var(--color-bg-secondary);
  color: var(--color-text-tertiary);
  cursor: not-allowed;
}

.form-input-sm {
  padding: 6px var(--spacing-sm);
  font-size: 0.8125rem;
}

.param-list {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.param-item {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  padding: var(--spacing-xs);
  background-color: var(--color-bg-secondary);
  border-radius: var(--radius-md);
}

.param-key {
  min-width: 80px;
  font-size: 0.75rem;
  font-weight: 500;
  color: var(--color-text-secondary);
  font-family: 'SF Mono', Monaco, 'Courier New', monospace;
}

.param-input {
  flex: 1;
  padding: 4px var(--spacing-sm);
  font-size: 0.75rem;
  color: var(--color-text-primary);
  background-color: var(--color-bg-primary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-sm);
}

.param-input:focus {
  outline: none;
  border-color: var(--color-primary);
}

.param-remove {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border: none;
  background: transparent;
  color: var(--color-text-tertiary);
  cursor: pointer;
  border-radius: var(--radius-sm);
  transition: all var(--transition-fast);
}

.param-remove:hover {
  background-color: var(--color-bg-tertiary);
  color: var(--color-error);
}

.add-param {
  display: flex;
  gap: var(--spacing-sm);
  margin-top: var(--spacing-md);
}

.add-param .form-input {
  flex: 1;
}

.btn-add-param {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  background-color: var(--color-bg-tertiary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-md);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-add-param:hover:not(:disabled) {
  background-color: var(--color-primary);
  border-color: var(--color-primary);
  color: var(--color-text-inverse);
}

.btn-add-param:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.quick-edit-actions {
  display: flex;
  gap: var(--spacing-sm);
  margin-top: var(--spacing-lg);
  padding-top: var(--spacing-md);
  border-top: 1px solid var(--color-border-muted);
}

/* Transitions */
.fade-enter-active,
.fade-leave-active {
  transition: opacity 150ms ease;
}

.fade-enter-active .quick-edit-form,
.fade-leave-active .quick-edit-form {
  transition: transform 150ms ease, opacity 150ms ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

.fade-enter-from .quick-edit-form,
.fade-leave-to .quick-edit-form {
  transform: scale(0.95);
  opacity: 0;
}

/* Dark mode */
@media (prefers-color-scheme: dark) {
  .quick-edit-overlay {
    background-color: rgba(0, 0, 0, 0.3);
  }
  
  .quick-edit-form {
    background-color: var(--color-bg-elevated);
  }
}
</style>
