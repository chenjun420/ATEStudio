<script setup lang="ts">
/**
 * LinkFaultContextMenu — 链路右键故障注入菜单（T30，设计文档 §8.3）。
 *
 * 在 FixtureDesigner 画布上右键链路（link edge）时弹出，提供 doc §8.3
 * 规定的 4 种故障类型；选择后由父组件转发 POST
 * /executions/{run_id}/fault-injection 到云端虚拟驱动。
 *
 * - 无活跃执行时整菜单禁用（disabled）
 * - 点击菜单外部 / Esc 关闭（emit 'close'）
 */
import { onBeforeUnmount, watch } from 'vue'
import { FAULT_TYPES, type LinkFaultType } from '@/composables/useFaultInjection'

const props = withDefaults(
  defineProps<{
    visible: boolean
    x: number
    y: number
    linkId: string
    disabled?: boolean
  }>(),
  { disabled: false },
)

const emit = defineEmits<{
  (e: 'select', faultType: LinkFaultType): void
  (e: 'close'): void
}>()

function onDocumentMouseDown(ev: MouseEvent) {
  const menu = document.getElementById('link-fault-context-menu')
  if (menu && ev.target instanceof Node && menu.contains(ev.target)) return
  emit('close')
}

function onDocumentKeydown(ev: KeyboardEvent) {
  if (ev.key === 'Escape') emit('close')
}

watch(
  () => props.visible,
  (visible) => {
    if (visible) {
      document.addEventListener('mousedown', onDocumentMouseDown)
      document.addEventListener('keydown', onDocumentKeydown)
    } else {
      document.removeEventListener('mousedown', onDocumentMouseDown)
      document.removeEventListener('keydown', onDocumentKeydown)
    }
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDocumentMouseDown)
  document.removeEventListener('keydown', onDocumentKeydown)
})

function choose(faultType: LinkFaultType) {
  if (props.disabled) return
  emit('select', faultType)
  emit('close')
}
</script>

<template>
  <ul
    v-if="visible"
    id="link-fault-context-menu"
    class="fault-context-menu"
    :class="{ disabled }"
    :style="{ left: `${x}px`, top: `${y}px` }"
    @contextmenu.prevent
  >
    <li class="menu-title">
      故障注入<span v-if="linkId" class="menu-link-id">{{ linkId }}</span>
    </li>
    <li v-for="t in FAULT_TYPES" :key="t.value">
      <button
        type="button"
        class="menu-item"
        :disabled="disabled"
        :data-fault-type="t.value"
        @click.stop="choose(t.value)"
      >
        {{ t.label }}
      </button>
    </li>
    <li v-if="disabled" class="menu-hint">无活跃执行，无法注入</li>
  </ul>
</template>

<style scoped>
.fault-context-menu {
  position: fixed;
  z-index: 3000;
  min-width: 200px;
  margin: 0;
  padding: 4px;
  list-style: none;
  background: var(--el-bg-color-overlay);
  border: 1px solid var(--el-border-color-light);
  border-radius: var(--el-border-radius-base);
  box-shadow: var(--el-box-shadow-light);
}
.menu-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 6px 10px;
  font-size: 12px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
  border-bottom: 1px solid var(--el-border-color-lighter);
  margin-bottom: 4px;
}
.menu-link-id {
  font-weight: 400;
  color: var(--el-text-color-placeholder);
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.menu-item {
  display: block;
  width: 100%;
  padding: 6px 10px;
  border: none;
  border-radius: var(--el-border-radius-base);
  background: transparent;
  text-align: left;
  font-size: 13px;
  color: var(--el-text-color-primary);
  cursor: pointer;
}
.menu-item:hover:not(:disabled) {
  background: var(--el-color-danger-light-9);
  color: var(--el-color-danger);
}
.menu-item:disabled {
  cursor: not-allowed;
  color: var(--el-text-color-placeholder);
}
.menu-hint {
  padding: 4px 10px 6px;
  font-size: 12px;
  color: var(--el-text-color-placeholder);
}
</style>
