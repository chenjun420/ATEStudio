<script setup lang="ts">
import type { SequenceTab } from '@/stores/tabs'

/**
 * SequenceTabs Component
 * 
 * A tab bar for managing multiple open sequences in the editor.
 * Supports:
 * - Multiple tabs with active state
 * - Dirty indicator for unsaved changes
 * - Close button for each tab
 * - New tab button
 */

// Props
defineProps<{
  /** Array of tabs to display */
  tabs: SequenceTab[]
  /** ID of the currently active tab */
  activeId: string | null
}>()

// Emits
const emit = defineEmits<{
  /** Emitted when a tab is clicked */
  select: [tabId: string]
  /** Emitted when a tab's close button is clicked */
  close: [tabId: string]
  /** Emitted when the new tab button is clicked */
  new: []
}>()
</script>

<template>
  <div class="tw-flex tw-items-center tw-gap-1 tw-px-2 tw-py-1.5 tw-bg-neutral-100 tw-border-b tw-border-neutral-200 tw-overflow-x-auto">
    <!-- Tab items -->
    <div
      v-for="tab in tabs"
      :key="tab.id"
      class="tw-group tw-flex tw-items-center tw-gap-2 tw-px-3 tw-py-1.5 tw-rounded-t-md tw-cursor-pointer tw-transition-all tw-whitespace-nowrap"
      :class="[
        activeId === tab.id
          ? 'tw-bg-white tw-text-primary-600 tw-border tw-border-b-0 tw-border-neutral-200 tw-shadow-sm'
          : 'tw-bg-transparent tw-text-neutral-600 hover:tw-bg-white/50'
      ]"
      @click="emit('select', tab.id)"
    >
      <!-- Tab name -->
      <span class="tw-text-sm tw-font-medium">
        {{ tab.name }}
      </span>

      <!-- Dirty indicator -->
      <span
        v-if="tab.isDirty"
        class="tw-text-warning tw-font-bold"
        title="Unsaved changes"
      >
        •
      </span>

      <!-- Close button -->
      <button
        class="tw-p-0.5 tw-rounded tw-opacity-0 group-hover:tw-opacity-100 hover:tw-bg-neutral-200 tw-transition-opacity tw-transition-colors"
        :class="{ 'tw-opacity-100': activeId === tab.id }"
        title="Close tab"
        @click.stop="emit('close', tab.id)"
      >
        <svg
          class="tw-w-3.5 tw-h-3.5"
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path
            stroke-linecap="round"
            stroke-linejoin="round"
            stroke-width="2"
            d="M6 18L18 6M6 6l12 12"
          />
        </svg>
      </button>
    </div>

    <!-- New tab button -->
    <button
      class="tw-flex tw-items-center tw-justify-center tw-w-7 tw-h-7 tw-rounded-md tw-text-neutral-500 hover:tw-bg-white hover:tw-text-neutral-700 tw-transition-colors"
      title="New sequence"
      @click="emit('new')"
    >
      <svg
        class="tw-w-4 tw-h-4"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          stroke-linecap="round"
          stroke-linejoin="round"
          stroke-width="2"
          d="M12 4v16m8-8H4"
        />
      </svg>
    </button>
  </div>
</template>