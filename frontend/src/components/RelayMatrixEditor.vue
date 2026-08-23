<script setup lang="ts">
/**
 * RelayMatrixEditor — 继电器矩阵编辑器（T27，设计文档 §8.3.2/§8.3.3）。
 *
 * 在 FixtureDesigner 属性面板中编辑选中夹具的继电器：
 *   - 按类型（spst/spdt/dpdt/matrix）添加继电器，渲染对应触点网格
 *   - 点击触点格绑定/解绑夹具端子（toggle）；端子占用校验阻断非法链路
 *   - matrix 类型支持行列增减的交叉点矩阵
 *   - 切换继电器 open/closed 状态、编辑控制信号
 *
 * 数据流：props 下行（fixture），事件上行（relays-change 携带新 relays 数组），
 * 由父视图写回 topology_data.fixtures[].relays 并随保存持久化。
 * 变换逻辑全部来自纯函数模块 utils/relayContacts.ts，组件不持有平行状态。
 */
import { computed, ref, watch } from 'vue'
import { ElButton, ElTag } from 'element-plus'

import type { Fixture, Relay } from '@/api/fixtures'
import {
  RELAY_TYPES,
  applyBind,
  applyToggleState,
  applyUnbind,
  applyUpdateRelay,
  boundTerminal,
  contactNamesForType,
  createRelay,
  findOccupant,
  matrixCellName,
  matrixSize,
  resizeMatrixContacts,
} from '@/utils/relayContacts'

// ─── Props / Emits ───────────────────────────────────────────────────────────

const props = defineProps<{
  /** 当前选中的夹具（含 terminals 与 relays）；null 时显示占位提示。 */
  fixture: Fixture | null
}>()

const emit = defineEmits<{
  (e: 'relays-change', relays: Relay[]): void
}>()

// ─── 内部状态 ────────────────────────────────────────────────────────────────

const selectedRelayId = ref('')
const pendingContact = ref('')
const lastError = ref('')

watch(
  () => props.fixture?.id,
  () => {
    selectedRelayId.value = ''
    pendingContact.value = ''
    lastError.value = ''
  },
)

const relays = computed<Relay[]>(() => props.fixture?.relays ?? [])

const selectedRelay = computed<Relay | null>(
  () => relays.value.find((r) => r.id === selectedRelayId.value) ?? relays.value[0] ?? null,
)

const selectedType = computed(() => selectedRelay.value?.type ?? 'spst')

const contactNames = computed<string[]>(() =>
  contactNamesForType(selectedType.value, selectedRelay.value?.contacts),
)

const isMatrix = computed(() => selectedType.value === 'matrix')

const matrixDims = computed(() => matrixSize(selectedRelay.value?.contacts))

/** 未被任何触点占用的夹具端子（绑定选择器只提供空闲端子）。 */
const availableTerminals = computed<Array<{ id: string; name?: string }>>(() =>
  (props.fixture?.terminals ?? []).filter((t) => !findOccupant(relays.value, t.id)),
)

function cellBoundTerminal(contact: string): string | null {
  return selectedRelay.value ? boundTerminal(selectedRelay.value, contact) : null
}

function gridStyle(): Record<string, string> {
  return { gridTemplateColumns: `repeat(${matrixDims.value.cols + 1}, minmax(0, 1fr))` }
}

// ─── 动作（全部经纯函数变换后整体上抛）─────────────────────────────────────

function emitChange(next: Array<Record<string, unknown>>) {
  lastError.value = ''
  pendingContact.value = ''
  emit('relays-change', next as unknown as Relay[])
}

function uniqueRelayId(base: string): string {
  const ids = new Set(relays.value.map((r) => r.id))
  if (!ids.has(base)) return base
  let i = 2
  while (ids.has(`${base}_${i}`)) i++
  return `${base}_${i}`
}

function addRelay(type: (typeof RELAY_TYPES)[number]) {
  const created = createRelay(relays.value.length, type)
  created.id = uniqueRelayId(created.id)
  const next = [...relays.value, created as unknown as Relay]
  selectedRelayId.value = String(created.id)
  emitChange(next)
}

function removeRelay(id: string) {
  emitChange(relays.value.filter((r) => r.id !== id))
  if (selectedRelayId.value === id) selectedRelayId.value = ''
}

function toggleStateFor(id: string) {
  emitChange(applyToggleState(relays.value, id))
}

function updateControlSignal(event: Event) {
  const relay = selectedRelay.value
  if (!relay) return
  const value = (event.target as HTMLInputElement).value.trim()
  if (!value) return // 后端要求 control_signal 非空
  emitChange(applyUpdateRelay(relays.value, relay.id, { control_signal: value }))
}

/** 点击触点格：已绑定 → 解绑；未绑定 → 进入待绑定（显示端子选择器）。 */
function onCellClick(contact: string) {
  const relay = selectedRelay.value
  if (!relay) return
  if (boundTerminal(relay, contact)) {
    emitChange(applyUnbind(relays.value, relay.id, contact))
    return
  }
  pendingContact.value = pendingContact.value === contact ? '' : contact
}

/**
 * 尝试绑定触点 → 端子。非法链路（端子已被其他触点占用）被阻断：
 * 不发事件、展示错误信息，返回 false。
 */
function requestBind(relayId: string, contact: string, terminalId: string): boolean {
  const res = applyBind(relays.value, relayId, contact, terminalId)
  if (!res.ok) {
    lastError.value = res.error
    return false
  }
  emitChange(res.relays)
  return true
}

function resizeMatrix(dRows: number, dCols: number) {
  const relay = selectedRelay.value
  if (!relay || relay.type !== 'matrix') return
  const size = matrixSize(relay.contacts)
  const rows = Math.max(1, size.rows + dRows)
  const cols = Math.max(1, size.cols + dCols)
  emitChange(
    applyUpdateRelay(relays.value, relay.id, {
      contacts: resizeMatrixContacts(relay.contacts, rows, cols),
    }),
  )
}

defineExpose({ requestBind })
</script>

<template>
  <div class="relay-matrix-editor">
    <!-- 类型添加栏 -->
    <div class="relay-toolbar">
      <el-button
        v-for="t in RELAY_TYPES"
        :key="t"
        class="relay-add"
        :data-type="t"
        size="small"
        plain
        @click="addRelay(t)"
      >
        + {{ t.toUpperCase() }}
      </el-button>
    </div>

    <p v-if="!fixture" class="relay-empty">请在画布中选择夹具节点后编辑继电器</p>
    <p v-else-if="relays.length === 0" class="relay-empty">暂无继电器，点击上方类型添加</p>

    <!-- 继电器列表 -->
    <ul v-else class="relay-list">
      <li
        v-for="r in relays"
        :key="r.id"
        :class="{ active: selectedRelay && r.id === selectedRelay.id }"
        @click="selectedRelayId = r.id"
      >
        <span class="relay-id">{{ r.id }}</span>
        <el-tag size="small" type="info">{{ r.type }}</el-tag>
        <button
          class="relay-state-toggle"
          :class="{ closed: r.state === 'closed' }"
          :title="r.state === 'closed' ? '点击断开' : '点击闭合'"
          @click.stop="toggleStateFor(r.id)"
        >
          {{ r.state === 'closed' ? '闭合' : '断开' }}
        </button>
        <button class="relay-remove" title="删除继电器" @click.stop="removeRelay(r.id)">×</button>
      </li>
    </ul>

    <!-- 选中继电器详情 -->
    <template v-if="selectedRelay">
      <div class="relay-detail">
        <label class="control-signal-row">
          <span>控制信号</span>
          <input
            class="relay-control-signal"
            :value="selectedRelay.control_signal ?? ''"
            placeholder="如 GPIO12 / MODBUS_REG3"
            @change="updateControlSignal"
          />
        </label>

        <!-- 固定类型：触点格 -->
        <div v-if="!isMatrix" class="contact-grid">
          <button
            v-for="name in contactNames"
            :key="name"
            class="contact-cell"
            :class="{ bound: cellBoundTerminal(name), pending: pendingContact === name }"
            :title="cellBoundTerminal(name) ? '点击解绑端子' : '点击绑定端子'"
            @click="onCellClick(name)"
          >
            <span class="cell-name">{{ name }}</span>
            <span class="cell-terminal">{{ cellBoundTerminal(name) ?? '未绑定' }}</span>
          </button>
        </div>

        <!-- matrix：交叉点网格 -->
        <template v-else>
          <div class="matrix-grid" :style="gridStyle()">
            <span class="mx-head corner"></span>
            <span v-for="c in matrixDims.cols" :key="`c${c}`" class="mx-head">C{{ c }}</span>
            <template v-for="ri in matrixDims.rows" :key="`r${ri}`">
              <span class="mx-head">R{{ ri }}</span>
              <button
                v-for="ci in matrixDims.cols"
                :key="`${ri}-${ci}`"
                class="matrix-cell"
                :class="{ bound: cellBoundTerminal(matrixCellName(ri, ci)), pending: pendingContact === matrixCellName(ri, ci) }"
                :data-contact="matrixCellName(ri, ci)"
                :title="cellBoundTerminal(matrixCellName(ri, ci)) ? '点击解绑端子' : '点击绑定端子'"
                @click="onCellClick(matrixCellName(ri, ci))"
              >
                {{ cellBoundTerminal(matrixCellName(ri, ci)) ?? '·' }}
              </button>
            </template>
          </div>
          <div class="matrix-resize">
            <el-button class="mx-row-add" size="small" @click="resizeMatrix(1, 0)">行+</el-button>
            <el-button class="mx-row-remove" size="small" :disabled="matrixDims.rows <= 1" @click="resizeMatrix(-1, 0)">行-</el-button>
            <el-button class="mx-col-add" size="small" @click="resizeMatrix(0, 1)">列+</el-button>
            <el-button class="mx-col-remove" size="small" :disabled="matrixDims.cols <= 1" @click="resizeMatrix(0, -1)">列-</el-button>
          </div>
        </template>

        <!-- 端子选择器（仅列出未被占用的端子） -->
        <div v-if="pendingContact" class="terminal-picker">
          <span class="picker-label">为 {{ pendingContact }} 选择端子：</span>
          <el-button
            v-for="t in availableTerminals"
            :key="t.id"
            class="terminal-option"
            :data-terminal="t.id"
            size="small"
            @click="requestBind(selectedRelay!.id, pendingContact, t.id)"
          >
            {{ t.name || t.id }}
          </el-button>
          <p v-if="availableTerminals.length === 0" class="picker-empty">无可用端子（均已被占用）</p>
        </div>

        <p v-if="lastError" class="relay-error">{{ lastError }}</p>
      </div>
    </template>
  </div>
</template>

<style scoped>
.relay-matrix-editor {
  font-size: 12px;
}
.relay-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: 8px;
}
.relay-empty {
  margin: 4px 0;
  color: var(--el-text-color-secondary);
}
.relay-list {
  list-style: none;
  margin: 0 0 8px;
  padding: 0;
}
.relay-list li {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px;
  margin-bottom: 4px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  cursor: pointer;
}
.relay-list li.active {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}
.relay-id {
  font-weight: 600;
  flex: 1;
}
.relay-state-toggle {
  border: 1px solid var(--el-border-color);
  border-radius: 10px;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-secondary);
  font-size: 11px;
  padding: 1px 8px;
  cursor: pointer;
}
.relay-state-toggle.closed {
  background: var(--el-color-success-light-9);
  border-color: var(--el-color-success);
  color: var(--el-color-success);
}
.relay-remove {
  border: none;
  background: transparent;
  color: var(--el-text-color-secondary);
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
}
.relay-remove:hover {
  color: var(--el-color-danger);
}
.relay-detail {
  border-top: 1px dashed var(--el-border-color-lighter);
  padding-top: 8px;
}
.control-signal-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
  color: var(--el-text-color-secondary);
}
.relay-control-signal {
  flex: 1;
  min-width: 0;
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
  padding: 3px 6px;
  font-size: 12px;
}
.contact-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(72px, 1fr));
  gap: 4px;
}
.contact-cell {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  padding: 5px 4px;
  border: 1px solid var(--el-border-color);
  border-radius: 6px;
  background: var(--el-bg-color);
  cursor: pointer;
}
.contact-cell .cell-name {
  font-weight: 600;
}
.contact-cell .cell-terminal {
  font-size: 11px;
  color: var(--el-text-color-secondary);
}
.contact-cell.bound {
  background: var(--el-color-primary-light-9);
  border-color: var(--el-color-primary);
}
.contact-cell.bound .cell-terminal {
  color: var(--el-color-primary);
}
.contact-cell.pending {
  border-style: dashed;
  border-color: var(--el-color-warning);
}
.matrix-grid {
  display: grid;
  gap: 3px;
  margin-bottom: 6px;
}
.mx-head {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  color: var(--el-text-color-secondary);
}
.matrix-cell {
  height: 30px;
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
  background: var(--el-bg-color);
  cursor: pointer;
  font-size: 11px;
  color: var(--el-text-color-secondary);
}
.matrix-cell.bound {
  background: var(--el-color-primary-light-9);
  border-color: var(--el-color-primary);
  color: var(--el-color-primary);
  font-weight: 600;
}
.matrix-cell.pending {
  border-style: dashed;
  border-color: var(--el-color-warning);
}
.matrix-resize {
  display: flex;
  gap: 4px;
  margin-bottom: 6px;
}
.terminal-picker {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
  padding: 6px;
  border: 1px dashed var(--el-border-color);
  border-radius: 6px;
  margin-top: 6px;
}
.picker-label {
  color: var(--el-text-color-secondary);
}
.picker-empty {
  margin: 0;
  color: var(--el-text-color-secondary);
}
.relay-error {
  margin: 6px 0 0;
  color: var(--el-color-danger);
}
</style>
