<script setup lang="ts">
/**
 * ScriptStepNode - Professional script step node for X6 graph
 * 
 * Features:
 * - 5 status styles: idle/running/passed/failed/error
 * - Input/output ports for connections
 * - Selection highlight effect
 * - Pulse animation for running state
 * - Hover edit button for quick editing
 */

import { computed, ref } from 'vue'

// Types
export type StepStatus = 'idle' | 'running' | 'passed' | 'failed' | 'error'

interface Props {
  /** Script name displayed on the node */
  scriptName?: string
  /** Step ID displayed below script name */
  stepId?: string
  /** Current status of the step */
  status?: StepStatus
  /** Whether the node is selected */
  selected?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  scriptName: 'Script',
  stepId: 'step-001',
  status: 'idle',
  selected: false,
})

// Emits
const emit = defineEmits<{
  edit: []
}>()

// Hover state for edit button
const isHovered = ref(false)

// Status color mapping using design tokens
const statusConfig = {
  idle: {
    border: 'var(--color-border-default)',
    bg: 'var(--color-bg-primary)',
    dot: 'var(--color-text-tertiary)',
  },
  running: {
    border: 'var(--color-info)',
    bg: 'var(--color-bg-primary)',
    dot: 'var(--color-info)',
  },
  passed: {
    border: 'var(--color-success)',
    bg: 'var(--color-bg-primary)',
    dot: 'var(--color-success)',
  },
  failed: {
    border: 'var(--color-error)',
    bg: 'var(--color-bg-primary)',
    dot: 'var(--color-error)',
  },
  error: {
    border: 'var(--color-warning)',
    bg: 'var(--color-bg-primary)',
    dot: 'var(--color-warning)',
  },
} as const

// Computed status styles
const statusStyles = computed(() => statusConfig[props.status])

// Check if running for pulse animation
const isRunning = computed(() => props.status === 'running')

// Handle edit button click
function handleEditClick(event: MouseEvent) {
  event.stopPropagation()
  emit('edit')
}
</script>

<template>
  <div 
    class="script-step-node"
    :class="{ 
      'is-selected': selected,
      'is-running': isRunning,
      'is-hovered': isHovered,
    }"
    :style="{
      '--node-border-color': statusStyles.border,
      '--node-bg-color': statusStyles.bg,
      '--status-dot-color': statusStyles.dot,
    }"
    @mouseenter="isHovered = true"
    @mouseleave="isHovered = false"
  >
    <!-- Input port (left side) -->
    <div class="port port-input">
      <div class="port-dot"></div>
    </div>
    
    <!-- Content area -->
    <div class="node-content">
      <!-- Status indicator -->
      <div class="status-indicator">
        <div 
          class="status-dot"
          :class="{ 'pulse': isRunning }"
        ></div>
      </div>
      
      <!-- Text content -->
      <div class="node-text">
        <span class="script-name">{{ scriptName }}</span>
        <span class="step-id">{{ stepId }}</span>
      </div>
      
      <!-- Edit button (visible on hover) -->
      <Transition name="fade">
        <button
          v-if="isHovered || selected"
          class="edit-button"
          @click="handleEditClick"
          title="Quick edit"
          aria-label="Quick edit"
        >
          <svg class="tw-w-4 tw-h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
          </svg>
        </button>
      </Transition>
    </div>
    
    <!-- Output port (right side) -->
    <div class="port port-output">
      <div class="port-dot"></div>
    </div>
  </div>
</template>

<style scoped>
.script-step-node {
  /* Node dimensions: 180x80px */
  width: 180px;
  height: 80px;
  
  /* Card styling */
  background-color: var(--node-bg-color);
  border: 2px solid var(--node-border-color);
  border-radius: var(--radius-xl);
  
  /* Layout */
  display: flex;
  align-items: center;
  position: relative;
  
  /* Transition for state changes */
  transition: border-color var(--transition-fast), 
              box-shadow var(--transition-fast);
  
  /* Shadow */
  box-shadow: var(--shadow-sm);
}

/* Selected state - highlight effect */
.script-step-node.is-selected {
  box-shadow: var(--shadow-glow),
              var(--shadow-md);
  border-width: 2px;
}

/* Running state - pulse animation */
.script-step-node.is-running {
  animation: nodePulse 1.5s ease-in-out infinite;
}

@keyframes nodePulse {
  0%, 100% {
    box-shadow: var(--shadow-sm);
  }
  50% {
    box-shadow: 0 0 20px rgba(59, 130, 246, 0.4),
                var(--shadow-md);
  }
}

/* Ports */
.port {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  width: 16px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.port-input {
  left: -8px;
}

.port-output {
  right: -8px;
}

.port-dot {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background-color: var(--color-bg-primary);
  border: 2px solid var(--node-border-color);
  transition: border-color var(--transition-fast),
              transform var(--transition-fast);
}

/* Port hover effect */
.port:hover .port-dot {
  transform: scale(1.2);
  border-color: var(--color-primary);
}

/* Content area */
.node-content {
  flex: 1;
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  padding: 0 var(--spacing-lg);
}

/* Status indicator */
.status-indicator {
  display: flex;
  align-items: center;
  justify-content: center;
}

.status-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background-color: var(--status-dot-color);
  transition: background-color var(--transition-fast);
}

/* Running pulse animation for status dot */
.status-dot.pulse {
  animation: dotPulse 1s ease-in-out infinite;
}

@keyframes dotPulse {
  0%, 100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.6;
    transform: scale(1.2);
  }
}

/* Text content */
.node-text {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  overflow: hidden;
}

.script-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.step-id {
  font-size: 11px;
  font-weight: 400;
  color: var(--color-text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* Dark mode adjustments */
@media (prefers-color-scheme: dark) {
  .script-step-node {
    background-color: var(--node-bg-color);
  }
  
  .port-dot {
    background-color: var(--color-bg-elevated);
  }
}

/* Edit button */
.edit-button {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  background-color: var(--color-bg-elevated);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-md);
  color: var(--color-text-tertiary);
  cursor: pointer;
  transition: all var(--transition-fast);
  flex-shrink: 0;
}

.edit-button:hover {
  background-color: var(--color-primary);
  border-color: var(--color-primary);
  color: var(--color-text-inverse);
  transform: scale(1.05);
}

.edit-button:active {
  transform: scale(0.95);
}

/* Fade transition for edit button */
.fade-enter-active,
.fade-leave-active {
  transition: opacity var(--transition-fast), transform var(--transition-fast);
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
  transform: scale(0.9);
}
</style>