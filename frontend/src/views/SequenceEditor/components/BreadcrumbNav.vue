<script setup lang="ts">
/**
 * BreadcrumbNav - Navigation breadcrumb for sub-graph view switching.
 * Shows path like "主图 > 循环容器名" and emits events for navigation.
 */

interface BreadcrumbItem {
  id: string
  label: string
}

interface Props {
  /** Ordered list of breadcrumb items from root to current */
  items: BreadcrumbItem[]
}

const props = defineProps<Props>()

const emit = defineEmits<{
  /** Emitted when a breadcrumb item is clicked (not the last/current one) */
  navigate: [id: string]
}>()

function handleClick(item: BreadcrumbItem, index: number) {
  // Don't navigate when clicking the current (last) item
  if (index >= props.items.length - 1) return
  emit('navigate', item.id)
}
</script>

<template>
  <nav v-if="items.length > 1" class="breadcrumb-nav" aria-label="Sub-graph navigation">
    <template v-for="(item, index) in items" :key="item.id">
      <span
        class="breadcrumb-item"
        :class="{ current: index === items.length - 1, clickable: index < items.length - 1 }"
        @click="handleClick(item, index)"
      >
        {{ item.label }}
      </span>
      <span v-if="index < items.length - 1" class="breadcrumb-separator">/</span>
    </template>
  </nav>
</template>

<style scoped>
.breadcrumb-nav {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 12px;
  background: var(--color-bg-elevated);
  border-bottom: 1px solid var(--color-border-default);
  font-size: 13px;
  min-height: 32px;
  flex-shrink: 0;
}

.breadcrumb-item {
  color: var(--color-text-secondary);
  white-space: nowrap;
}

.breadcrumb-item.clickable {
  color: var(--color-primary);
  cursor: pointer;
  transition: color 150ms ease;
}

.breadcrumb-item.clickable:hover {
  color: var(--color-primary-dark);
  text-decoration: underline;
}

.breadcrumb-item.current {
  color: var(--color-text-primary);
  font-weight: 600;
  cursor: default;
}

.breadcrumb-separator {
  color: var(--color-text-tertiary);
  user-select: none;
}
</style>
