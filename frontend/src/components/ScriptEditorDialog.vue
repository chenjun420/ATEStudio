<script setup lang="ts">
import { ref, computed, watch, onBeforeUnmount, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import type * as Monaco from 'monaco-editor'
import MonacoEditor from '@/components/MonacoEditor.vue'
import {
  fetchScriptContent,
  updateScriptContent,
  fetchScriptVersions,
  fetchScriptVersionContent,
  type ScriptVersionInfo,
} from '@/api/scripts'

// Props
interface Props {
  /** Whether the dialog is visible */
  visible: boolean
  /** Script ID to edit */
  scriptId: string | null
  /** Script name for display in title */
  scriptName?: string
}

const props = defineProps<Props>()

// Emits
const emit = defineEmits<{
  'update:visible': [value: boolean]
  'saved': []
}>()

// Dialog visibility computed (v-model pattern)
const dialogVisible = computed({
  get: () => props.visible,
  set: (value) => emit('update:visible', value),
})

// State
const isLoading = ref(false)
const isSaving = ref(false)
const scriptContent = ref('')
const originalContent = ref('')
const commitMessage = ref('')
const versions = ref<ScriptVersionInfo[]>([])
const selectedVersionHash = ref<string | null>(null)
const versionContent = ref<string | null>(null)
const isLoadingVersion = ref(false)
const showDiffView = ref(false)

// Diff editor refs
const diffEditorContainer = ref<HTMLDivElement>()
let diffEditor: Monaco.editor.IStandaloneDiffEditor | null = null
let monacoModule: typeof Monaco | null = null

// Detect dark mode
const isDarkMode = computed(() => {
  return document.documentElement.classList.contains('dark')
})

const effectiveTheme = computed(() => {
  return isDarkMode.value ? 'vs-dark' : 'vs'
})

// Version display label
const versionOptions = computed(() => {
  return versions.value.map((v) => ({
    value: v.hash,
    label: `${v.hash.substring(0, 8)} - ${v.message} (${v.author}, ${formatDate(v.timestamp)})`,
  }))
})

/**
 * Format ISO timestamp to readable string
 */
function formatDate(timestamp: string): string {
  try {
    const date = new Date(timestamp)
    return date.toLocaleDateString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return timestamp
  }
}

/**
 * Load script content and version history
 */
async function loadScriptData() {
  if (!props.scriptId) return

  isLoading.value = true
  commitMessage.value = ''
  selectedVersionHash.value = null
  versionContent.value = null
  showDiffView.value = false

  try {
    const [contentResponse, versionsResponse] = await Promise.all([
      fetchScriptContent(props.scriptId),
      fetchScriptVersions(props.scriptId),
    ])

    scriptContent.value = contentResponse.content
    originalContent.value = contentResponse.content
    versions.value = versionsResponse.versions
  } catch (err) {
    const message = err instanceof Error ? err.message : 'Failed to load script content'
    ElMessage.error(message)
  } finally {
    isLoading.value = false
  }
}

/**
 * Save script content
 */
async function handleSave() {
  if (!props.scriptId) return

  isSaving.value = true
  try {
    const response = await updateScriptContent(props.scriptId, {
      content: scriptContent.value,
      commit_message: commitMessage.value || undefined,
    })

    originalContent.value = response.content
    commitMessage.value = ''
    ElMessage.success('Script saved successfully')
    emit('saved')

    // Refresh version history
    const versionsResponse = await fetchScriptVersions(props.scriptId)
    versions.value = versionsResponse.versions
  } catch (err) {
    const message = err instanceof Error ? err.message : 'Failed to save script'
    ElMessage.error(message)
  } finally {
    isSaving.value = false
  }
}

/**
 * Load content for a specific version
 */
async function loadVersionContent(hash: string) {
  if (!props.scriptId) return

  isLoadingVersion.value = true
  try {
    const response = await fetchScriptVersionContent(props.scriptId, hash)
    versionContent.value = response.content
    showDiffView.value = true

    // Initialize diff editor after DOM update
    await nextTick()
    await initDiffEditor()
  } catch (err) {
    const message = err instanceof Error ? err.message : 'Failed to load version content'
    ElMessage.error(message)
    showDiffView.value = false
  } finally {
    isLoadingVersion.value = false
  }
}

/**
 * Initialize Monaco diff editor
 */
async function initDiffEditor() {
  // Dispose existing diff editor
  disposeDiffEditor()

  if (!diffEditorContainer.value || versionContent.value === null) return

  const monaco = await import('monaco-editor')
  monacoModule = monaco

  const originalModel = monaco.editor.createModel(versionContent.value, 'python')
  const modifiedModel = monaco.editor.createModel(scriptContent.value, 'python')

  diffEditor = monaco.editor.createDiffEditor(diffEditorContainer.value, {
    theme: effectiveTheme.value,
    readOnly: false,
    renderSideBySide: true,
    scrollBeyondLastLine: false,
    minimap: { enabled: false },
    automaticLayout: true,
    fontSize: 14,
    padding: { top: 8, bottom: 8 },
  })

  diffEditor.setModel({
    original: originalModel,
    modified: modifiedModel,
  })

  // Listen for changes in the modified (right) side
  diffEditor.getModifiedEditor().onDidChangeModelContent(() => {
    if (diffEditor) {
      scriptContent.value = diffEditor.getModifiedEditor().getValue()
    }
  })
}

/**
 * Dispose diff editor
 */
function disposeDiffEditor() {
  if (diffEditor) {
    const originalModel = diffEditor.getModel()?.original
    const modifiedModel = diffEditor.getModel()?.modified
    originalModel?.dispose()
    modifiedModel?.dispose()
    diffEditor.dispose()
    diffEditor = null
  }
  monacoModule = null
}

/**
 * Handle version selection change
 */
function handleVersionChange(hash: string | null) {
  if (hash) {
    loadVersionContent(hash)
  } else {
    showDiffView.value = false
    versionContent.value = null
    disposeDiffEditor()
  }
}

/**
 * Handle save from MonacoEditor component
 */
function handleEditorSave() {
  handleSave()
}

/**
 * Handle dialog close
 */
function handleClose() {
  disposeDiffEditor()
  showDiffView.value = false
  selectedVersionHash.value = null
  versionContent.value = null
  dialogVisible.value = false
}

// Watch for dialog visibility to load data
watch(dialogVisible, (visible) => {
  if (visible && props.scriptId) {
    loadScriptData()
  } else if (!visible) {
    disposeDiffEditor()
  }
})

// Watch for dark mode changes to update diff editor theme
let darkModeObserver: MutationObserver | null = null

watch(dialogVisible, (visible) => {
  if (visible) {
    darkModeObserver = new MutationObserver(() => {
      if (monacoModule) {
        monacoModule.editor.setTheme(isDarkMode.value ? 'vs-dark' : 'vs')
      }
    })
    darkModeObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['class'],
    })
  } else {
    darkModeObserver?.disconnect()
    darkModeObserver = null
  }
})

onBeforeUnmount(() => {
  disposeDiffEditor()
  darkModeObserver?.disconnect()
})
</script>

<template>
  <el-dialog
    v-model="dialogVisible"
    :title="`Edit Script: ${scriptName || scriptId || ''}`"
    fullscreen
    :close-on-click-modal="false"
    class="script-editor-dialog"
    @close="handleClose"
  >
    <!-- Toolbar -->
    <template #header>
      <div class="dialog-header">
        <span class="dialog-title">Edit Script: {{ scriptName || scriptId || '' }}</span>
        <div class="header-actions">
          <!-- Version selector -->
          <el-select
            v-model="selectedVersionHash"
            placeholder="Select version..."
            clearable
            size="small"
            class="version-select"
            @change="handleVersionChange"
          >
            <el-option
              v-for="opt in versionOptions"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>

          <!-- Diff view toggle -->
          <el-button
            v-if="selectedVersionHash"
            size="small"
            :type="showDiffView ? 'primary' : 'default'"
            @click="() => { if (selectedVersionHash) loadVersionContent(selectedVersionHash) }"
          >
            {{ showDiffView ? 'Diff View' : 'Show Diff' }}
          </el-button>
        </div>
      </div>
    </template>

    <!-- Loading state -->
    <div v-if="isLoading" class="editor-loading">
      <el-icon class="is-loading" :size="32">
        <svg viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
          <path d="M512 64a32 32 0 0 1 32 32v192a32 32 0 0 1-64 0V96a32 32 0 0 1 32-32zm0 640a32 32 0 0 1 32 32v192a32 32 0 0 1-64 0V736a32 32 0 0 1 32-32zm448-192a32 32 0 0 1-32 32H736a32 32 0 0 1 0-64h192a32 32 0 0 1 32 32zM288 512a32 32 0 0 1-32 32H64a32 32 0 0 1 0-64h192a32 32 0 0 1 32 32z" fill="currentColor" />
        </svg>
      </el-icon>
      <span>Loading script content...</span>
    </div>

    <!-- Editor content -->
    <div v-else class="editor-content">
      <!-- Diff editor view -->
      <div v-if="showDiffView" class="diff-editor-wrapper">
        <div v-if="isLoadingVersion" class="editor-loading">
          <el-icon class="is-loading" :size="32">
            <svg viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
              <path d="M512 64a32 32 0 0 1 32 32v192a32 32 0 0 1-64 0V96a32 32 0 0 1 32-32zm0 640a32 32 0 0 1 32 32v192a32 32 0 0 1-64 0V736a32 32 0 0 1 32-32zm448-192a32 32 0 0 1-32 32H736a32 32 0 0 1 0-64h192a32 32 0 0 1 32 32zM288 512a32 32 0 0 1-32 32H64a32 32 0 0 1 0-64h192a32 32 0 0 1 32 32z" fill="currentColor" />
            </svg>
          </el-icon>
          <span>Loading version content...</span>
        </div>
        <div v-else ref="diffEditorContainer" class="diff-editor-container" />
      </div>

      <!-- Normal editor view -->
      <div v-else class="normal-editor-wrapper">
        <MonacoEditor
          v-model="scriptContent"
          language="python"
          @save="handleEditorSave"
        />
      </div>
    </div>

    <!-- Footer -->
    <template #footer>
      <div class="dialog-footer">
        <!-- Commit message input -->
        <el-input
          v-model="commitMessage"
          placeholder="Commit message (optional)"
          size="small"
          class="commit-input"
          @keyup.enter="handleSave"
        />

        <div class="footer-actions">
          <el-button @click="handleClose">Cancel</el-button>
          <el-button
            type="primary"
            :loading="isSaving"
            :disabled="scriptContent === originalContent && !commitMessage"
            @click="handleSave"
          >
            Save
          </el-button>
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.script-editor-dialog :deep(.el-dialog) {
  display: flex;
  flex-direction: column;
}

.script-editor-dialog :deep(.el-dialog__body) {
  flex: 1;
  padding: 0;
  overflow: hidden;
}

.dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  gap: 16px;
}

.dialog-title {
  font-size: 1.125rem;
  font-weight: 600;
  color: var(--color-text-primary, #1f2937);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.version-select {
  width: 320px;
}

.editor-loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  min-height: 300px;
  gap: 12px;
  color: var(--color-text-secondary, #6b7280);
  font-size: 0.875rem;
}

.editor-content {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.normal-editor-wrapper {
  flex: 1;
  min-height: 0;
}

.diff-editor-wrapper {
  flex: 1;
  min-height: 0;
}

.diff-editor-container {
  width: 100%;
  height: 100%;
  min-height: 400px;
}

.dialog-footer {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
}

.commit-input {
  flex: 1;
}

.footer-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}
</style>
