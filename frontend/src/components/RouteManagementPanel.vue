<script setup lang="ts">
/**
 * RouteManagementPanel — 信号路由管理面板（T28，设计文档 §8.3.2 Route 实体）。
 *
 * 在 FixtureDesigner 属性面板中对 topology_data.routes 做 CRUD 与激活：
 *   - 列表展示全部路由（名称、激活徽标、链路/继电器计数），点击选中
 *   - 新建（自动分配唯一 ROUTE_{n} id）、重命名、删除
 *   - 为选中路由指派组成链路 / 需闭合的继电器（chip 点击切换）
 *   - 激活/停用：仅翻转该路由自身的 active 标志——绝不自动激活其他
 *     路由，加载拓扑时也不做任何隐式置位
 *   - 绑定关联测试步骤（associated_step）；被步骤引用的路由删除前
 *     必须经 ElMessageBox 确认
 *
 * 数据流：props 下行（routes/links/relays），事件上行（routes-change
 * 携带新 routes 数组），由父视图写回 topology_data.routes 并随保存持久化。
 * 变换逻辑全部来自纯函数模块 utils/routes.ts，组件不持有平行状态。
 */
import { computed, ref, watch } from 'vue'
import { ElButton, ElInput, ElMessageBox, ElTag } from 'element-plus'

import type { Link, Relay, Route } from '@/api/fixtures'
import {
  applyAssignLinks,
  applyAssignRelays,
  applyAssociatedStep,
  applyCreate,
  applyDelete,
  applyRename,
  applySetActive,
  createRoute,
  routeReferencedByStep,
  uniqueRouteId,
  type RouteLike,
} from '@/utils/routes'

// ─── Props / Emits ───────────────────────────────────────────────────────────

const props = defineProps<{
  /** 当前拓扑的全部路由（topology_data.routes）。 */
  routes: Route[]
  /** 可指派的链路清单（topology_data.links）。 */
  links: Link[]
  /** 可指派的继电器清单（各夹具 relays 的扁平汇总）。 */
  relays: Relay[]
}>()

const emit = defineEmits<{
  (e: 'routes-change', routes: Route[]): void
}>()

// ─── 内部状态 ────────────────────────────────────────────────────────────────

const selectedRouteId = ref('')
const newRouteName = ref('')
const renameDraft = ref('')
const stepDraft = ref('')
const lastError = ref('')

const routes = computed<RouteLike[]>(() => props.routes ?? [])

const selectedRoute = computed<RouteLike | null>(
  () => routes.value.find((r) => r.id === selectedRouteId.value) ?? routes.value[0] ?? null,
)

/**
 * 最近一次上抛的 routes 快照。父视图写回 props 前存在一拍延迟，
 * 选择守卫用它识别"自己刚创建/刚操作的路由"，避免把有效选区误判为失效。
 */
let lastEmittedRoutes: readonly RouteLike[] | null = null

// 选择或外部数据变化时同步草稿；清掉指向已删除路由的选区。
watch(
  () => [props.routes, selectedRouteId.value] as const,
  () => {
    const inProps = routes.value.some((r) => r.id === selectedRouteId.value)
    const inLastEmission =
      lastEmittedRoutes?.some((r) => r.id === selectedRouteId.value) ?? false
    if (selectedRouteId.value && !inProps && !inLastEmission) {
      selectedRouteId.value = ''
    }
    renameDraft.value = selectedRoute.value?.name ?? ''
    stepDraft.value = selectedRoute.value?.associated_step ?? ''
  },
  { immediate: true },
)

function isLinkAssigned(linkId: string): boolean {
  return (selectedRoute.value?.links ?? []).includes(linkId)
}

function isRelayAssigned(relayId: string): boolean {
  return (selectedRoute.value?.relays ?? []).includes(relayId)
}

// ─── 动作（全部经纯函数变换后整体上抛）─────────────────────────────────────

function emitChange(next: RouteLike[]) {
  lastError.value = ''
  lastEmittedRoutes = next
  emit('routes-change', next as unknown as Route[])
}

/** 新建路由：唯一 id、默认未激活（绝不自动激活）。 */
function addRoute() {
  const name = newRouteName.value.trim()
  const created = createRoute(routes.value.length, name)
  created.id = uniqueRouteId(routes.value)
  emitChange(applyCreate(routes.value, created))
  selectedRouteId.value = created.id
  newRouteName.value = ''
}

function commitRename(value: string) {
  const route = selectedRoute.value
  if (!route) return
  const name = value.trim()
  if (!name) {
    lastError.value = '路由名称不能为空'
    renameDraft.value = route.name ?? ''
    return
  }
  emitChange(applyRename(routes.value, route.id, name))
}

function toggleLink(linkId: string) {
  const route = selectedRoute.value
  if (!route) return
  const current = route.links ?? []
  const next = current.includes(linkId)
    ? current.filter((l) => l !== linkId)
    : [...current, linkId]
  emitChange(applyAssignLinks(routes.value, route.id, next))
}

function toggleRelay(relayId: string) {
  const route = selectedRoute.value
  if (!route) return
  const current = route.relays ?? []
  const next = current.includes(relayId)
    ? current.filter((r) => r !== relayId)
    : [...current, relayId]
  emitChange(applyAssignRelays(routes.value, route.id, next))
}

/** 激活/停用：仅翻转当前路由自身的 active 位。 */
function toggleActive() {
  const route = selectedRoute.value
  if (!route) return
  emitChange(applySetActive(routes.value, route.id, !route.active))
}

function commitStep(value: string) {
  const route = selectedRoute.value
  if (!route) return
  emitChange(applyAssociatedStep(routes.value, route.id, value))
}

/**
 * 删除当前路由。被测试步骤引用（associated_step 非空）时必须先经
 * 用户确认；取消则不发出任何变更并提示。
 */
async function requestDelete() {
  const route = selectedRoute.value
  if (!route) return
  if (routeReferencedByStep(route)) {
    try {
      await ElMessageBox.confirm(
        `路由「${route.name || route.id}」已被步骤 ${route.associated_step} 引用，确认删除？`,
        '删除确认',
        { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
      )
    } catch {
      lastError.value = '已取消删除'
      return
    }
  }
  emitChange(applyDelete(routes.value, route.id))
  if (selectedRouteId.value === route.id) selectedRouteId.value = ''
}

defineExpose({ requestDelete })
</script>

<template>
  <div class="route-management-panel">
    <!-- 新建栏 -->
    <div class="route-create">
      <el-input
        v-model="newRouteName"
        class="route-new-name"
        size="small"
        placeholder="新路由名称"
        @keyup.enter="addRoute"
      />
      <el-button class="route-add" size="small" type="primary" plain @click="addRoute">
        + 路由
      </el-button>
    </div>

    <p v-if="routes.length === 0" class="route-empty">暂无路由，点击上方按钮新建</p>

    <!-- 路由列表 -->
    <ul v-else class="route-list">
      <li
        v-for="r in routes"
        :key="r.id"
        class="route-item"
        :class="{ selected: selectedRoute && r.id === selectedRoute.id, active: r.active }"
        :data-route="r.id"
        @click="selectedRouteId = r.id"
      >
        <span class="route-item-name">{{ r.name || r.id }}</span>
        <el-tag v-if="r.active" class="route-active-badge" size="small" type="success">激活</el-tag>
        <span class="route-counts">{{ (r.links ?? []).length }}链路 · {{ (r.relays ?? []).length }}继电器</span>
      </li>
    </ul>

    <!-- 选中路由编辑器 -->
    <template v-if="selectedRoute">
      <div class="route-editor">
        <label class="route-field">
          <span>名称</span>
          <el-input
            v-model="renameDraft"
            class="route-rename-input"
            size="small"
            placeholder="路由名称"
            @change="commitRename"
          />
        </label>

        <span class="route-field-label">组成链路</span>
        <div class="route-links">
          <button
            v-for="l in links"
            :key="l.id"
            class="route-link-option"
            :class="{ selected: isLinkAssigned(l.id) }"
            :data-link="l.id"
            :title="`${l.signal_type} · ${isLinkAssigned(l.id) ? '点击移出' : '点击加入'}`"
            @click="toggleLink(l.id)"
          >
            {{ l.id }}
          </button>
          <span v-if="links.length === 0" class="route-hint">暂无链路</span>
        </div>

        <span class="route-field-label">闭合继电器</span>
        <div class="route-relays">
          <button
            v-for="rly in relays"
            :key="rly.id"
            class="route-relay-option"
            :class="{ selected: isRelayAssigned(rly.id) }"
            :data-relay="rly.id"
            :title="isRelayAssigned(rly.id) ? '点击移出' : '点击加入'"
            @click="toggleRelay(rly.id)"
          >
            {{ rly.id }}
          </button>
          <span v-if="relays.length === 0" class="route-hint">暂无继电器</span>
        </div>

        <label class="route-field">
          <span>关联步骤</span>
          <el-input
            v-model="stepDraft"
            class="route-step-input"
            size="small"
            placeholder="测试步骤 ID（留空解除）"
            @change="commitStep"
          />
        </label>

        <div class="route-actions">
          <el-button
            class="route-activate"
            size="small"
            :type="selectedRoute.active ? 'warning' : 'success'"
            plain
            @click="toggleActive"
          >
            {{ selectedRoute.active ? '停用' : '激活' }}
          </el-button>
          <el-button class="route-delete" size="small" type="danger" plain @click="requestDelete">
            删除
          </el-button>
        </div>

        <p v-if="lastError" class="route-error">{{ lastError }}</p>
      </div>
    </template>
  </div>
</template>

<style scoped>
.route-management-panel {
  font-size: 12px;
}
.route-create {
  display: flex;
  gap: 4px;
  margin-bottom: 8px;
}
.route-new-name {
  flex: 1;
  min-width: 0;
}
.route-empty {
  margin: 4px 0;
  color: var(--el-text-color-secondary);
}
.route-list {
  list-style: none;
  margin: 0 0 8px;
  padding: 0;
}
.route-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px;
  margin-bottom: 4px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  cursor: pointer;
}
.route-item.selected {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}
.route-item.active:not(.selected) {
  border-color: var(--el-color-success-light-5);
}
.route-item-name {
  font-weight: 600;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.route-counts {
  font-size: 11px;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
}
.route-editor {
  border-top: 1px dashed var(--el-border-color-lighter);
  padding-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.route-field {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--el-text-color-secondary);
}
.route-field > span {
  flex-shrink: 0;
}
.route-field .el-input {
  flex: 1;
  min-width: 0;
}
.route-field-label {
  color: var(--el-text-color-secondary);
}
.route-links,
.route-relays {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.route-link-option,
.route-relay-option {
  border: 1px solid var(--el-border-color);
  border-radius: 10px;
  background: var(--el-bg-color);
  color: var(--el-text-color-regular);
  font-size: 11px;
  padding: 2px 8px;
  cursor: pointer;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
}
.route-link-option.selected,
.route-relay-option.selected {
  background: var(--el-color-primary-light-9);
  border-color: var(--el-color-primary);
  color: var(--el-color-primary);
  font-weight: 600;
}
.route-hint {
  color: var(--el-text-color-secondary);
}
.route-actions {
  display: flex;
  gap: 4px;
}
.route-error {
  margin: 0;
  color: var(--el-color-danger);
}
</style>
