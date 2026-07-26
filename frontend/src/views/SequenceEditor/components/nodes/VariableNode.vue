<script setup lang="ts">
import { ref, computed } from 'vue'
import { Delete, Plus } from '@element-plus/icons-vue'

/**
 * VariableNode - Variable definition node for SequenceEditor
 * 
 * Displays a list of key-value pairs for defining sequence-wide variables.
 * Supports add, edit, and delete operations via Element Plus components.
 */

// Props definition
interface Variable {
  key: string
  value: string
}

interface Props {
  nodeId?: string
  variables?: Variable[]
}

const props = withDefaults(defineProps<Props>(), {
  nodeId: '',
  variables: () => [],
})

// Emits
const emit = defineEmits<{
  (e: 'update:variables', value: Variable[]): void
}>()

// Local state for variables (mutable copy)
const localVariables = ref<Variable[]>([...props.variables])

// Computed count display
const variableCount = computed(() => localVariables.value.length)

/**
 * Add a new variable row
 */
function addVariable() {
  const newVar: Variable = { key: '', value: '' }
  localVariables.value.push(newVar)
  emit('update:variables', localVariables.value)
}

/**
 * Delete a variable by index
 */
function deleteVariable(index: number) {
  localVariables.value.splice(index, 1)
  emit('update:variables', localVariables.value)
}

/**
 * Handle key change
 */
function updateKey(index: number, newKey: string) {
  localVariables.value[index].key = newKey
  emit('update:variables', localVariables.value)
}

/**
 * Handle value change
 */
function updateValue(index: number, newValue: string) {
  localVariables.value[index].value = newValue
  emit('update:variables', localVariables.value)
}
</script>

<template>
  <div class="variable-node">
    <!-- Header -->
    <div class="variable-node__header">
      <span class="variable-node__icon">V</span>
      <span class="variable-node__title">Variables</span>
      <span class="variable-node__count">{{ variableCount }}</span>
    </div>

    <!-- Variable list -->
    <div class="variable-node__content">
      <div
        v-for="(variable, index) in localVariables"
        :key="index"
        class="variable-node__row"
      >
        <el-input
          :model-value="variable.key"
          placeholder="Key"
          size="small"
          class="variable-node__input"
          @update:model-value="updateKey(index, $event)"
        />
        <span class="variable-node__separator">=</span>
        <el-input
          :model-value="variable.value"
          placeholder="Value"
          size="small"
          class="variable-node__input"
          @update:model-value="updateValue(index, $event)"
        />
        <el-button
          type="danger"
          :icon="Delete"
          size="small"
          class="variable-node__delete"
          @click="deleteVariable(index)"
        />
      </div>

      <!-- Add button -->
      <el-button
        type="primary"
        :icon="Plus"
        size="small"
        class="variable-node__add"
        @click="addVariable"
      >
        Add Variable
      </el-button>
    </div>
  </div>
</template>

<style scoped>
.variable-node {
  width: 200px;
  height: 120px;
  background-color: var(--color-bg-elevated);
  border: 2px solid var(--color-accent-green);
  border-radius: var(--radius-lg);
  font-size: 12px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.variable-node__header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  background-color: var(--color-accent-green);
  color: var(--color-text-inverse);
  font-weight: 600;
}

.variable-node__icon {
  width: 18px;
  height: 18px;
  display: flex;
  align-items: center;
  justify-content: center;
  background-color: rgba(255, 255, 255, 0.2);
  border-radius: 4px;
  font-size: 11px;
  font-weight: 700;
}

.variable-node__title {
  flex: 1;
}

.variable-node__count {
  background-color: rgba(255, 255, 255, 0.25);
  padding: 2px 6px;
  border-radius: 10px;
  font-size: 11px;
}

.variable-node__content {
  flex: 1;
  padding: 6px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.variable-node__row {
  display: flex;
  align-items: center;
  gap: 4px;
}

.variable-node__input {
  flex: 1;
  min-width: 0;
}

.variable-node__separator {
  color: var(--color-text-tertiary);
  font-weight: 600;
}

.variable-node__delete {
  padding: 4px;
  min-width: auto;
}

.variable-node__add {
  width: 100%;
  margin-top: 4px;
}
</style>