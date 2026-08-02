<script setup lang="ts">
import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import MonacoEditor from '@/components/MonacoEditor.vue'
import axios from 'axios'

// Props
interface Props {
  /** Whether the dialog is visible */
  visible: boolean
  /** Product type for context */
  productType?: string
}

const props = withDefaults(defineProps<Props>(), {
  productType: '',
})

// Emits
const emit = defineEmits<{
  'update:visible': [value: boolean]
  'use-script': [code: string]
}>()

// Dialog visibility computed (v-model pattern)
const dialogVisible = computed({
  get: () => props.visible,
  set: (value) => emit('update:visible', value),
})

// API client
const api = axios.create({
  baseURL: '/api/v1',
  timeout: 60000,
  headers: { 'Content-Type': 'application/json' },
})

// ─── State ───────────────────────────────────────────────────────────────

interface ScriptGenerateResponse {
  code: string
  confidence: number
  validation_errors: string[]
  suggestions: string[]
}

interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  timestamp: string
}

const specText = ref('')
const productTypeInput = ref(props.productType || '')
const generatedCode = ref('')
const confidence = ref(0)
const validationErrors = ref<string[]>([])
const suggestions = ref<string[]>([])
const isGenerating = ref(false)
const isRefining = ref(false)
const chatMessages = ref<ChatMessage[]>([])
const refineFeedback = ref('')
const activeTab = ref<'editor' | 'chat'>('editor')

// ─── Methods ──────────────────────────────────────────────────────────────

/** Format timestamp for chat messages */
function now(): string {
  return new Date().toLocaleTimeString()
}

/** Generate a script from the spec text */
async function handleGenerate(): Promise<void> {
  if (!specText.value.trim()) {
    ElMessage.warning('Please enter a test specification')
    return
  }
  if (!productTypeInput.value.trim()) {
    ElMessage.warning('Please enter a product type')
    return
  }

  isGenerating.value = true
  chatMessages.value = []
  try {
    const response = await api.post<ScriptGenerateResponse>('/scripts/generate', {
      spec_text: specText.value,
      product_type: productTypeInput.value,
    })
    const data = response.data
    generatedCode.value = data.code
    confidence.value = data.confidence
    validationErrors.value = data.validation_errors || []
    suggestions.value = data.suggestions || []

    chatMessages.value.push({
      role: 'assistant',
      content: `Script generated with ${(data.confidence * 100).toFixed(0)}% confidence.${
        data.validation_errors.length > 0
          ? ` Found ${data.validation_errors.length} validation error(s).`
          : ''
      }`,
      timestamp: now(),
    })

    if (data.validation_errors.length === 0) {
      ElMessage.success('Script generated successfully')
    } else {
      ElMessage.warning('Script generated with validation errors')
    }
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : 'Generation failed'
    ElMessage.error(message)
    chatMessages.value.push({
      role: 'assistant',
      content: `Error: ${message}`,
      timestamp: now(),
    })
  } finally {
    isGenerating.value = false
  }
}

/** Refine the script based on user feedback */
async function handleRefine(): Promise<void> {
  if (!refineFeedback.value.trim()) {
    ElMessage.warning('Please enter feedback')
    return
  }
  if (!generatedCode.value) {
    ElMessage.warning('Please generate a script first')
    return
  }

  isRefining.value = true
  const feedback = refineFeedback.value
  refineFeedback.value = ''

  // Add user message to chat
  chatMessages.value.push({
    role: 'user',
    content: feedback,
    timestamp: now(),
  })

  try {
    const response = await api.post<ScriptGenerateResponse>('/scripts/refine', {
      code: generatedCode.value,
      feedback: feedback,
      product_type: productTypeInput.value,
    })
    const data = response.data
    generatedCode.value = data.code
    confidence.value = data.confidence
    validationErrors.value = data.validation_errors || []
    suggestions.value = data.suggestions || []

    chatMessages.value.push({
      role: 'assistant',
      content: `Script refined with ${(data.confidence * 100).toFixed(0)}% confidence.${
        data.validation_errors.length > 0
          ? ` Found ${data.validation_errors.length} validation error(s).`
          : ''
      }`,
      timestamp: now(),
    })

    ElMessage.success('Script refined')
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : 'Refinement failed'
    ElMessage.error(message)
    chatMessages.value.push({
      role: 'assistant',
      content: `Error: ${message}`,
      timestamp: now(),
    })
  } finally {
    isRefining.value = false
  }
}

/** Use the generated script — emit to parent */
function handleUseScript(): void {
  if (!generatedCode.value) {
    ElMessage.warning('No script to use')
    return
  }
  emit('use-script', generatedCode.value)
  dialogVisible.value = false
}

/** Handle Enter key in refine input */
function handleRefineEnter(): void {
  if (!isRefining.value) {
    handleRefine()
  }
}

/** Close and reset */
function handleClose(): void {
  dialogVisible.value = false
}

// Watch for productType prop changes
watch(
  () => props.productType,
  (newVal) => {
    if (newVal && !productTypeInput.value) {
      productTypeInput.value = newVal
    }
  },
)

// Reset state when dialog opens
watch(dialogVisible, (visible) => {
  if (visible) {
    if (props.productType) {
      productTypeInput.value = props.productType
    }
  }
})

onBeforeUnmount(() => {
  chatMessages.value = []
})
</script>

<template>
  <el-dialog
    v-model="dialogVisible"
    title="AI Script Generator"
    fullscreen
    :close-on-click-modal="false"
    class="script-generator-dialog"
    @close="handleClose"
  >
    <!-- Top: spec input area -->
    <div class="spec-section">
      <div class="spec-row">
        <el-input
          v-model="productTypeInput"
          placeholder="Product Type (e.g. COMM-DEV-001)"
          class="product-type-input"
        />
        <el-button
          type="primary"
          :loading="isGenerating"
          @click="handleGenerate"
        >
          {{ isGenerating ? 'Generating...'' : 'Generate Script' }}
        </el-button>
      </div>
      <el-input
        v-model="specText"
        type="textarea"
        :rows="2"
        placeholder="Enter test specification in natural language (e.g. 'power on 5V rail, check I2C communication')"
        class="spec-input"
      />
    </div>

    <!-- Main content: editor + chat panel -->
    <div class="main-content">
      <!-- Tabs: editor / chat -->
      <el-tabs v-model="activeTab" class="content-tabs">
        <el-tab-pane label="Generated Code" name="editor">
          <!-- Confidence + validation indicators -->
          <div v-if="generatedCode" class="meta-bar">
            <el-tag :type="confidence > 0.7 ? 'success' : confidence > 0.3 ? 'warning' : 'danger'" size="small">
              Confidence: {{ (confidence * 100).toFixed(0) }}%
            </el-tag>
            <el-tag v-if="validationErrors.length === 0" type="success" size="small">
              No errors
            </el-tag>
            <el-tag v-else type="danger" size="small">
              {{ validationErrors.length }} error(s)
            </el-tag>
          </div>

          <!-- Monaco Editor or textarea fallback -->
          <div class="editor-wrapper">
            <MonacoEditor
              v-if="generatedCode"
              v-model="generatedCode"
              language="python"
            />
            <div v-else class="editor-placeholder">
              <el-icon :size="48" color="#909399">
                <svg viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
                  <path d="M832 128H192c-35.3 0-64 28.7-64 64v640c0 35.3 28.7 64 64 64h640c35.3 0 64-28.7 64-64V192c0-35.3-28.7-64-64-64zm0 704H192V192h640v640z" fill="currentColor" />
                  <path d="M320 384h384v64H320zM320 512h384v64H320zM320 640h256v64H320z" fill="currentColor" />
                </svg>
              </el-icon>
              <span>Generated script will appear here</span>
            </div>
          </div>

          <!-- Validation errors -->
          <div v-if="validationErrors.length > 0" class="validation-errors">
            <div class="errors-header">Validation Errors:</div>
            <div v-for="(err, i) in validationErrors" :key="i" class="error-item">
              <el-icon color="#f56c6c"><Warning /></el-icon>
              <span>{{ err }}</span>
            </div>
          </div>

          <!-- Suggestions -->
          <div v-if="suggestions.length > 0" class="suggestions">
            <div class="suggestions-header">Suggestions:</div>
            <div v-for="(sug, i) in suggestions" :key="i" class="suggestion-item">
              <el-icon color="#e6a23c"><InfoFilled /></el-icon>
              <span>{{ sug }}</span>
            </div>
          </div>
        </el-tab-pane>

        <el-tab-pane label="Chat (Iterate)" name="chat">
          <div class="chat-panel">
            <div class="chat-messages">
              <div
                v-for="(msg, i) in chatMessages"
                :key="i"
                :class="['chat-message', msg.role]"
              >
                <div class="message-avatar">
                  {{ msg.role === 'user' ? 'U' : 'AI' }}
                </div>
                <div class="message-content">
                  <div class="message-text">{{ msg.content }}</div>
                  <div class="message-time">{{ msg.timestamp }}</div>
                </div>
              </div>
              <div v-if="chatMessages.length === 0" class="chat-empty">
                Start a conversation to refine your script.
                <br />
                Example: "add retry logic for I2C communication"
              </div>
            </div>
            <div class="chat-input-area">
              <el-input
                v-model="refineFeedback"
                type="textarea"
                :rows="2"
                placeholder="Enter feedback to refine the script..."
                :disabled="isRefining || !generatedCode"
                @keyup.ctrl.enter="handleRefineEnter"
              />
              <el-button
                type="primary"
                :loading="isRefining"
                :disabled="!generatedCode || !refineFeedback.trim()"
                @click="handleRefine"
              >
                {{ isRefining ? 'Refining...' : 'Refine' }}
              </el-button>
            </div>
          </div>
        </el-tab-pane>
      </el-tabs>
    </div>

    <!-- Footer -->
    <template #footer>
      <div class="dialog-footer">
        <el-button @click="handleClose">Cancel</el-button>
        <el-button
          type="success"
          :disabled="!generatedCode"
          @click="handleUseScript"
        >
          Use This Script
        </el-button>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.script-generator-dialog :deep(.el-dialog) {
  display: flex;
  flex-direction: column;
}

.script-generator-dialog :deep(.el-dialog__body) {
  flex: 1;
  padding: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.spec-section {
  padding: 12px 16px;
  border-bottom: 1px solid var(--el-border-color);
  display: flex;
  flex-direction: column;
  gap: 8px;
  flex-shrink: 0;
}

.spec-row {
  display: flex;
  gap: 12px;
  align-items: center;
}

.product-type-input {
  max-width: 300px;
}

.spec-input {
  width: 100%;
}

.main-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-height: 0;
}

.content-tabs {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.content-tabs :deep(.el-tabs__content) {
  flex: 1;
  overflow: hidden;
  display: flex;
}

.content-tabs :deep(.el-tab-pane) {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.meta-bar {
  display: flex;
  gap: 8px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--el-border-color);
  flex-shrink: 0;
}

.editor-wrapper {
  flex: 1;
  min-height: 300px;
  overflow: hidden;
}

.editor-placeholder {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: var(--el-text-color-secondary);
  font-size: 0.875rem;
}

.validation-errors {
  padding: 8px 16px;
  background: var(--el-color-danger-light-9);
  border-top: 1px solid var(--el-color-danger-light-7);
  flex-shrink: 0;
  max-height: 150px;
  overflow-y: auto;
}

.errors-header {
  font-weight: 600;
  color: var(--el-color-danger);
  margin-bottom: 4px;
}

.error-item {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  padding: 2px 0;
  font-size: 0.875rem;
  color: var(--el-text-color-regular);
}

.suggestions {
  padding: 8px 16px;
  background: var(--el-color-warning-light-9);
  border-top: 1px solid var(--el-color-warning-light-7);
  flex-shrink: 0;
  max-height: 150px;
  overflow-y: auto;
}

.suggestions-header {
  font-weight: 600;
  color: var(--el-color-warning);
  margin-bottom: 4px;
}

.suggestion-item {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  padding: 2px 0;
  font-size: 0.875rem;
  color: var(--el-text-color-regular);
}

.chat-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.chat-message {
  display: flex;
  gap: 12px;
}

.chat-message.user {
  flex-direction: row-reverse;
}

.message-avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.875rem;
  font-weight: 600;
  flex-shrink: 0;
  background: var(--el-color-primary-light-7);
  color: var(--el-color-primary);
}

.chat-message.user .message-avatar {
  background: var(--el-color-success-light-7);
  color: var(--el-color-success);
}

.message-content {
  max-width: 70%;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.chat-message.user .message-content {
  align-items: flex-end;
}

.message-text {
  padding: 8px 12px;
  border-radius: 8px;
  background: var(--el-fill-color-light);
  font-size: 0.875rem;
  line-height: 1.5;
}

.chat-message.user .message-text {
  background: var(--el-color-success-light-9);
}

.message-time {
  font-size: 0.75rem;
  color: var(--el-text-color-placeholder);
}

.chat-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  color: var(--el-text-color-secondary);
  font-size: 0.875rem;
  line-height: 1.8;
}

.chat-input-area {
  padding: 12px 16px;
  border-top: 1px solid var(--el-border-color);
  display: flex;
  gap: 12px;
  align-items: flex-end;
  flex-shrink: 0;
}

.chat-input-area :deep(.el-textarea) {
  flex: 1;
}

.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  width: 100%;
}
</style>
