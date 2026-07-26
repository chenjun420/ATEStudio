<script setup lang="ts">
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import type * as Monaco from 'monaco-editor'

// Props
interface Props {
  /** Editor content (v-model) */
  modelValue: string
  /** Programming language for syntax highlighting */
  language?: string
  /** Whether the editor is read-only */
  readOnly?: boolean
  /** Editor theme ('vs-dark' | 'vs' | 'hc-black') */
  theme?: string
}

const props = withDefaults(defineProps<Props>(), {
  language: 'python',
  readOnly: false,
  theme: undefined,
})

// Emits
const emit = defineEmits<{
  'update:modelValue': [value: string]
  'save': []
}>()

// Refs
const editorContainer = ref<HTMLDivElement>()
let editor: Monaco.editor.IStandaloneCodeEditor | null = null
let monacoInstance: typeof Monaco | null = null
let isUpdatingFromProp = false
let resizeObserver: ResizeObserver | null = null

// Detect dark mode from document
const isDarkMode = computed(() => {
  return document.documentElement.classList.contains('dark')
})

// Resolve effective theme
const effectiveTheme = computed(() => {
  if (props.theme) return props.theme
  return isDarkMode.value ? 'vs-dark' : 'vs'
})

// v-model compatible computed
const editorContent = computed({
  get: () => props.modelValue,
  set: (value: string) => emit('update:modelValue', value),
})

/**
 * Initialize Monaco editor
 */
async function initEditor() {
  if (!editorContainer.value) return

  // Dynamic import to avoid blocking initial page load
  const monaco = await import('monaco-editor')
  monacoInstance = monaco

  editor = monaco.editor.create(editorContainer.value, {
    value: props.modelValue,
    language: props.language,
    readOnly: props.readOnly,
    theme: effectiveTheme.value,
    automaticLayout: false,
    minimap: { enabled: true },
    scrollBeyondLastLine: false,
    fontSize: 14,
    lineNumbers: 'on',
    renderLineHighlight: 'line',
    tabSize: 4,
    insertSpaces: true,
    wordWrap: 'on',
    folding: true,
    bracketPairColorization: { enabled: true },
    padding: { top: 8, bottom: 8 },
  })

  // Listen for content changes
  editor.onDidChangeModelContent(() => {
    if (!isUpdatingFromProp && editor) {
      const value = editor.getValue()
      editorContent.value = value
    }
  })

  // Register Ctrl+S save action
  editor.addAction({
    id: 'editor-save',
    label: 'Save',
    keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS],
    contextMenuGroupId: 'navigation',
    contextMenuOrder: 1,
    run: () => {
      emit('save')
    },
  })

  // Setup ResizeObserver for auto-resize
  resizeObserver = new ResizeObserver(() => {
    editor?.layout()
  })
  resizeObserver.observe(editorContainer.value)
}

// Watch for external modelValue changes
watch(() => props.modelValue, (newValue) => {
  if (!editor) return
  const currentValue = editor.getValue()
  if (newValue !== currentValue) {
    isUpdatingFromProp = true
    const model = editor.getModel()
    if (model) {
      // Preserve cursor position
      const position = editor.getPosition()
      model.setValue(newValue)
      if (position) {
        editor.setPosition(position)
      }
    }
    isUpdatingFromProp = false
  }
})

// Watch for language changes
watch(() => props.language, (newLang) => {
  if (!editor || !monacoInstance) return
  const model = editor.getModel()
  if (model) {
    monacoInstance.editor.setModelLanguage(model, newLang)
  }
})

// Watch for readOnly changes
watch(() => props.readOnly, (newReadOnly) => {
  editor?.updateOptions({ readOnly: newReadOnly })
})

// Watch for theme changes
watch(effectiveTheme, (newTheme) => {
  monacoInstance?.editor.setTheme(newTheme)
})

// Watch for dark mode class changes on document
let darkModeObserver: MutationObserver | null = null

onMounted(async () => {
  await nextTick()
  await initEditor()

  // Watch for dark mode class changes on <html>
  darkModeObserver = new MutationObserver(() => {
    // Trigger re-evaluation of effectiveTheme computed
    if (monacoInstance && !props.theme) {
      monacoInstance.editor.setTheme(isDarkMode.value ? 'vs-dark' : 'vs')
    }
  })
  darkModeObserver.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['class'],
  })
})

onBeforeUnmount(() => {
  // Cleanup editor
  editor?.dispose()
  editor = null
  monacoInstance = null

  // Cleanup observers
  resizeObserver?.disconnect()
  resizeObserver = null
  darkModeObserver?.disconnect()
  darkModeObserver = null
})

// Expose editor instance for parent access
defineExpose({
  /** Get the Monaco editor instance */
  getEditor: () => editor,
  /** Get the Monaco module instance */
  getMonaco: () => monacoInstance,
  /** Focus the editor */
  focus: () => editor?.focus(),
  /** Layout the editor (call after container resize) */
  layout: () => editor?.layout(),
})
</script>

<template>
  <div ref="editorContainer" class="monaco-editor-container" />
</template>

<style scoped>
.monaco-editor-container {
  width: 100%;
  height: 100%;
  min-height: 200px;
}
</style>
