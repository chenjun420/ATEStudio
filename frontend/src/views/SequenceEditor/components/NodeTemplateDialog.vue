<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { ElDialog, ElInput, ElButton, ElFormItem, ElForm } from 'element-plus'
import { useNodeTemplate } from '@/composables/useNodeTemplate'
import type { Node } from '@antv/x6'

// Props
interface Props {
  visible: boolean
  node: Node | null
}

const props = defineProps<Props>()

// Emits
const emit = defineEmits<{
  'update:visible': [value: boolean]
  'saved': [templateId: string]
}>

// Composable
const { createTemplateFromNode, isLoading } = useNodeTemplate()

// Form data
const templateName = ref('')
const templateDescription = ref('')
const formRef = ref()

// Dialog visibility computed
const dialogVisible = computed({
  get: () => props.visible,
  set: (value) => emit('update:visible', value),
})

// Form validation rules
const rules = {
  name: [
    { required: true, message: 'Template name is required', trigger: 'blur' },
    { min: 2, max: 100, message: 'Name must be 2-100 characters', trigger: 'blur' },
  ],
}

// Reset form when dialog opens
watch(dialogVisible, (visible) => {
  if (visible) {
    templateName.value = ''
    templateDescription.value = ''
  }
})

// Node type label for display
const nodeTypeLabel = computed(() => {
  if (!props.node) return 'Unknown'
  const data = props.node.getData() as Record<string, unknown>
  const type = data?.nodeType || props.node.shape || 'script-step'
  return String(type).replace(/-/g, ' ').replace(/\b\w/g, l => l.toUpperCase())
})

/**
 * Handle form submission
 */
async function handleSubmit() {
  if (!props.node) return

  try {
    await formRef.value?.validate()

    const template = await createTemplateFromNode(props.node, templateName.value)
    
    emit('saved', template.id)
    dialogVisible.value = false
  } catch (error) {
    console.error('Failed to create template:', error)
  }
}

/**
 * Handle dialog close
 */
function handleClose() {
  dialogVisible.value = false
}
</script>

<template>
  <ElDialog
    v-model="dialogVisible"
    title="Save as Template"
    width="480px"
    :close-on-click-modal="false"
    @close="handleClose"
  >
    <ElForm
      ref="formRef"
      :model="{ name: templateName }"
      :rules="rules"
      label-position="top"
      class="template-form"
    >
      <!-- Node Info -->
      <div class="node-info-section">
        <div class="info-item">
          <span class="info-label">Node Type</span>
          <span class="info-value">{{ nodeTypeLabel }}</span>
        </div>
        <div v-if="node" class="info-item">
          <span class="info-label">Node ID</span>
          <span class="info-value mono">{{ node.id }}</span>
        </div>
      </div>

      <!-- Template Name -->
      <ElFormItem label="Template Name" prop="name">
        <ElInput
          v-model="templateName"
          placeholder="Enter a name for this template"
          :disabled="isLoading"
        />
      </ElFormItem>

      <!-- Description (Optional) -->
      <ElFormItem label="Description (Optional)">
        <ElInput
          v-model="templateDescription"
          type="textarea"
          :rows="3"
          placeholder="Brief description of this template"
          :disabled="isLoading"
        />
      </ElFormItem>

      <!-- Info Note -->
      <div class="info-note">
        <svg class="info-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
        <span>The node's appearance and properties will be saved as a reusable template.</span>
      </div>
    </ElForm>

    <!-- Footer Actions -->
    <template #footer>
      <div class="dialog-footer">
        <ElButton @click="handleClose">Cancel</ElButton>
        <ElButton
          type="primary"
          :loading="isLoading"
          :disabled="!templateName.trim()"
          @click="handleSubmit"
        >
          Save Template
        </ElButton>
      </div>
    </template>
  </ElDialog>
</template>

<style scoped>
.template-form {
  padding: var(--spacing-sm) 0;
}

.node-info-section {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
  padding: var(--spacing-md);
  background-color: var(--color-bg-secondary);
  border-radius: var(--radius-lg);
  margin-bottom: var(--spacing-lg);
}

.info-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.info-label {
  font-size: 0.75rem;
  font-weight: 500;
  color: var(--color-text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.info-value {
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--color-text-primary);
}

.info-value.mono {
  font-family: 'SF Mono', 'Monaco', 'Consolas', monospace;
  font-size: 0.75rem;
  font-weight: 500;
}

.info-note {
  display: flex;
  align-items: flex-start;
  gap: var(--spacing-sm);
  padding: var(--spacing-sm) var(--spacing-md);
  background-color: var(--color-bg-tertiary);
  border-radius: var(--radius-md);
  font-size: 0.75rem;
  color: var(--color-text-secondary);
  line-height: 1.5;
}

.info-icon {
  width: 1rem;
  height: 1rem;
  flex-shrink: 0;
  color: var(--color-primary);
  margin-top: 0.125rem;
}

.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: var(--spacing-sm);
}

/* Element Plus overrides */
:deep(.el-dialog__header) {
  padding: var(--spacing-lg);
  border-bottom: 1px solid var(--color-border-default);
  margin: 0;
}

:deep(.el-dialog__title) {
  font-size: 1.125rem;
  font-weight: 600;
  color: var(--color-text-primary);
}

:deep(.el-dialog__body) {
  padding: var(--spacing-lg);
}

:deep(.el-dialog__footer) {
  padding: var(--spacing-md) var(--spacing-lg);
  border-top: 1px solid var(--color-border-default);
}

:deep(.el-form-item__label) {
  font-size: 0.875rem;
  font-weight: 500;
  color: var(--color-text-primary);
}

:deep(.el-input__wrapper),
:deep(.el-textarea__inner) {
  box-shadow: 0 0 0 1px var(--color-border-default) inset;
  border-radius: var(--radius-md);
  transition: box-shadow var(--transition-fast);
}

:deep(.el-input__wrapper:hover),
:deep(.el-textarea__inner:hover) {
  box-shadow: 0 0 0 1px var(--color-primary) inset;
}

:deep(.el-input__wrapper.is-focus),
:deep(.el-textarea__inner:focus) {
  box-shadow: 0 0 0 1px var(--color-primary) inset;
}
</style>
