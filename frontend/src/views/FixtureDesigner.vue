<script setup lang="ts">
/**
 * FixtureDesigner — 工装设计调试器（设计文档 §8.3）。
 *
 * 功能：
 *   - 拖拽式搭建 仪器→夹具→DUT 完整接线拓扑（X6 拓扑画布）
 *   - 设备库（内置常用模板 + 云端设备模板库）
 *   - 8 类接线校验（§8.3.5，POST /fixtures/{id}/validate）
 *   - 运行时状态高亮（§8.3.6：活跃链路流动动画、仪器/夹具/继电器/测量值）
 *   - 故障定位高亮（§8.3.7：suspect_links 红闪 + 故障详情面板）
 *   - 保存/导出 JSON/YAML/复制/版本历史
 *
 * Route: /flow/fixture-designer
 */
import { computed, nextTick, onMounted, ref, shallowRef, watch } from 'vue'
import {
  ElButton,
  ElCard,
  ElDialog,
  ElForm,
  ElFormItem,
  ElInput,
  ElMessage,
  ElMessageBox,
  ElOption,
  ElSelect,
  ElTable,
  ElTableColumn,
  ElTag,
  ElTooltip,
  ElEmpty,
} from 'element-plus'
import { ArrowDown } from '@element-plus/icons-vue'
import type { Graph } from '@antv/x6'
import { useGraph } from '@/composables/useGraph'
import { useTopologyRuntimeStore } from '@/stores/topologyRuntime'
import RelayMatrixEditor from '@/components/RelayMatrixEditor.vue'
import RouteManagementPanel from '@/components/RouteManagementPanel.vue'
import LinkFaultContextMenu from '@/components/LinkFaultContextMenu.vue'
import { useFaultInjection, type LinkFaultType } from '@/composables/useFaultInjection'
import {
  createFixtureTopology,
  downloadFixtureExport,
  duplicateFixtureTopology,
  exportFixtureTopology,
  getFixtureTopology,
  listDeviceTemplates,
  listFixtureTopologies,
  listFixtureVersions,
  updateFixtureTopology,
  validateFixtureTopology,
  type FixtureDeviceTemplate,
  type FixtureTopologyData,
  type FixtureTopologyResponse,
  type Relay,
  type Route,
  type ValidationResult,
} from '@/api/fixtures'
import { listExecutions, type ExecutionListItem } from '@/api/executions'
import { applyFixtureTierLayout } from '@/utils/fixtureLayoutAdapter'
import { LINK_STYLES, clearRouteHighlight, syncRouteHighlight } from '@/utils/routeHighlightAdapter'

const topologyStore = useTopologyRuntimeStore()
const { graph } = useGraph('fixture-canvas')

// ─── 状态 ──────────────────────────────────────────────────────────────────

const loading = ref(false)
const listDialogVisible = ref(false)
const topologies = ref<FixtureTopologyResponse[]>([])
const current = ref<FixtureTopologyResponse | null>(null)
const currentData = ref<FixtureTopologyData | null>(null)

// 运行时
const executions = ref<ExecutionListItem[]>([])
const selectedRunId = ref('')
const runtimeConnected = ref(false)

// 校验
const validation = ref<ValidationResult | null>(null)
const validating = ref(false)

// 新建
const createDialogVisible = ref(false)
const createForm = ref({
  name: '',
  version: '1.0',
  product_model: '',
  description: '',
})

// 设备库
const library = ref<Array<{ kind: string; label: string; icon: string }>>([
  { kind: 'psu', label: '电源 PSU', icon: '⚡' },
  { kind: 'dmm', label: '万用表 DMM', icon: '📟' },
  { kind: 'eload', label: '电子负载 ELoad', icon: '🔌' },
  { kind: 'fixture', label: '夹具 Fixture', icon: '🛠️' },
  { kind: 'dut', label: 'DUT 被测件', icon: '📦' },
])
const templates = ref<FixtureDeviceTemplate[]>([])

// 选中元素属性
const selectedInfo = ref<{
  kind: 'instrument' | 'fixture' | 'dut' | 'link' | 'none'
  id: string
  name: string
  detail: string
}>({ kind: 'none', id: '', name: '', detail: '' })

// 布局按钮用于重新排布
const layoutMode = ref('grid')

// 继电器矩阵编辑（T27）：选中夹具节点时在属性面板编辑其 relays
const selectedFixture = computed<Fixture | null>(() => {
  if (selectedInfo.value.kind !== 'fixture') return null
  return currentData.value?.fixtures.find((f) => f.id === selectedInfo.value.id) ?? null
})

function onRelaysChange(relays: Relay[]) {
  const fixture = selectedFixture.value
  if (!fixture) return
  fixture.relays = relays
}

// 信号路由管理（T28）：编辑 currentData.routes，激活路由高亮其组成链路
const allRelays = computed<Relay[]>(() =>
  (currentData.value?.fixtures ?? []).flatMap((f) => f.relays ?? []),
)

function onRoutesChange(routes: Route[]) {
  const data = currentData.value
  if (!data) {
    ElMessage.warning('请先新建或打开一个工装配置')
    return
  }
  data.routes = routes
  nextTick(() => highlightActiveRoutes())
}

/**
 * T32：路由高亮——唯一激活路由的路径（links+relays）强调描边，其余链路/继电器
 * 压暗；故障样式优先（fault 链路不参与高亮/压暗）。纯计算见 utils/routeHighlight.ts。
 */
function highlightActiveRoutes() {
  const g = graph.value
  const data = currentData.value
  if (!g || !data) return
  syncRouteHighlight(g, { routes: data.routes, links: data.links, relays: allRelays.value })
}

/** 运行开始即清除路由高亮（§8.3 clear on run start：运行时状态样式接管）。 */
function clearRouteHighlightOnRunStart() {
  const g = graph.value
  const data = currentData.value
  if (!g || !data) return
  clearRouteHighlight(g, [...data.links.map((l) => l.id), ...allRelays.value.map((r) => r.id)])
}

// ─── X6 画布 ──────────────────────────────────────────────────────────────

const NODE_KIND_STYLES: Record<string, { fill: string; stroke: string; labelColor: string }> = {
  instrument: { fill: '#ECF5FF', stroke: '#409EFF', labelColor: '#174EA6' },
  fixture: { fill: '#FDF6EC', stroke: '#E6A23C', labelColor: '#7A5900' },
  dut: { fill: '#F0F9EB', stroke: '#67C23A', labelColor: '#1E6B34' },
}

function renderTopology(data: FixtureTopologyData) {
  const g = graph.value
  if (!g) return
  g.clearCells()

  let idx = 0
  const gridW = 220
  const gridH = 140

  // 仪器 → 左列
  for (const inst of data.instruments) {
    const p = inst.position ?? { x: 60, y: 60 + idx * gridH }
    addTopoNode(g, 'instrument', inst.id, inst.name || inst.id, inst.type, p, inst)
    idx++
  }
  idx = 0
  for (const fix of data.fixtures) {
    const p = fix.position ?? { x: 300, y: 60 + idx * gridH }
    addTopoNode(g, 'fixture', fix.id, fix.name || fix.id, fix.version ?? '', p, fix)
    idx++
  }
  idx = 0
  for (const dut of data.duts) {
    const p = dut.position ?? { x: 540, y: 60 + idx * gridH }
    addTopoNode(g, 'dut', dut.id, dut.product_model || dut.id, dut.id, p, dut)
    idx++
  }

  // 链路
  for (const link of data.links) {
    const style = LINK_STYLES[link.signal_type] ?? LINK_STYLES.signal
    const attrs: Record<string, unknown> = {
      line: {
        stroke: style.stroke,
        strokeWidth: style.strokeWidth,
        targetMarker: { name: 'block', size: 8 },
        ...(style.dash ? { strokeDasharray: style.dash } : {}),
      },
    }
    g.addEdge({
      id: link.id,
      source: { cell: link.from.entity_id, port: link.from.port_id },
      target: { cell: link.to.entity_id, port: link.to.port_id },
      attrs,
      zIndex: 1,
      data: { kind: 'link', linkId: link.id, signalType: link.signal_type, status: link.status ?? 'idle' },
    })
  }
}

function addTopoNode(
  g: Graph,
  kind: 'instrument' | 'fixture' | 'dut',
  id: string,
  name: string,
  sub: string,
  pos: { x: number; y: number },
  entity: Record<string, unknown>,
) {
  const style = NODE_KIND_STYLES[kind]
  // 为每条链路端点生成端口
  let portEntities: Array<{ id: string; name: string }> = []
  if (kind === 'instrument') {
    portEntities = ((entity.channels as Array<{ id: string; name?: string }>) ?? []).map((c) => ({
      id: c.id,
      name: c.name ?? c.id,
    }))
  } else if (kind === 'fixture') {
    portEntities = ((entity.terminals as Array<{ id: string; name?: string }>) ?? []).map((t) => ({
      id: t.id,
      name: t.name ?? t.id,
    }))
  } else {
    portEntities = ((entity.test_points as Array<{ id: string; net?: string }>) ?? []).map((t) => ({
      id: t.id,
      name: t.net ?? t.id,
    }))
  }

  const portItems = portEntities.map((p) => ({ id: p.id, group: 'io' }))
  const label = kind === 'dut' ? `${name}\n${sub}` : `${name}\n${sub}`

  g.addNode({
    id,
    shape: 'rect',
    x: pos.x,
    y: pos.y,
    width: 160,
    height: Math.max(64, 24 + portItems.length * 14),
    attrs: {
      body: { fill: style.fill, stroke: style.stroke, strokeWidth: 2, rx: 8, ry: 8 },
      label: { fill: style.labelColor, fontSize: 13, fontWeight: 600 },
    },
    ports: {
      groups: {
        io: {
          position: { name: 'right' },
          attrs: { circle: { r: 4, magnet: true, stroke: '#909399', strokeWidth: 2, fill: '#fff' } },
        },
      },
      items: portItems,
    },
    data: { kind, entityId: id, name, label, status: 'idle' },
  })
}

/** T29：dagre 分层自动布局（仪器上→夹具中→DUT 下）。仅按钮触发，数据变化不自动重排。 */
function applyTierLayout() {
  const g = graph.value
  if (!g) return
  if (!applyFixtureTierLayout(g)) return // 空画布 no-op
}

/** 旧版网格布局（按列排布），保留于布局菜单。 */
function applyGridLayout() {
  const g = graph.value
  if (!g) return
  const colX: Record<string, number> = { instrument: 60, fixture: 320, dut: 580 }
  const colY: Record<string, number> = { instrument: 60, fixture: 60, dut: 60 }
  for (const kind of ['instrument', 'fixture', 'dut'] as const) {
    graphNodesOfKind(g, kind).forEach((id, i) => {
      const node = g.getCellById(id)
      if (node && node.isNode()) {
        node.position(colX[kind], colY[kind] + i * 150)
      }
    })
  }
}

function onLayoutCommand(command: string) {
  if (command === 'auto') applyTierLayout()
  else if (command === 'grid') applyGridLayout()
}

function graphNodesOfKind(g: Graph, kind: string): string[] {
  return g
    .getNodes()
    .filter((n) => (n.getData() as { kind?: string })?.kind === kind)
    .map((n) => n.id)
}

// ─── 选中属性 ──────────────────────────────────────────────────────────────

function onNodeSelected(id: string) {
  const g = graph.value
  if (!g) return
  const node = g.getCellById(id)
  if (!node || !node.isNode()) return
  const d = (node.getData() ?? {}) as Record<string, unknown>
  selectedInfo.value = {
    kind: (d.kind as 'instrument' | 'fixture' | 'dut') ?? 'none',
    id,
    name: String(d.name ?? id),
    detail: String(d.label ?? ''),
  }
}

// ─── 运行时状态高亮（§8.3.6）──────────────────────────────────────────────

function applyLinkRuntimeState(linkId: string, status: string, active: boolean) {
  const g = graph.value
  if (!g) return
  const edge = g.getCellById(linkId)
  if (!edge || !edge.isEdge()) return
  const d = (edge.getData() ?? {}) as { signalType?: string }
  const base = LINK_STYLES[d.signalType ?? 'signal'] ?? LINK_STYLES.signal
  if (status === 'fault') {
    edge.attr('line/stroke', '#F56C6C')
    edge.attr('line/strokeWidth', 4)
  } else if (active) {
    edge.attr('line/stroke', base.stroke)
    edge.attr('line/strokeWidth', base.strokeWidth + 1)
    edge.attr('line/strokeDasharray', base.dash ?? '6 3')
  } else {
    edge.attr('line/stroke', base.stroke)
    edge.attr('line/strokeWidth', base.strokeWidth)
    edge.attr('line/strokeDasharray', base.dash ?? '')
    edge.attr('line/opacity', 0.6)
  }
}

function applyNodeRuntimeState(entityId: string, kind: string, status: string) {
  const g = graph.value
  if (!g) return
  const node = g.getCellById(entityId)
  if (!node || !node.isNode()) return
  const style = NODE_KIND_STYLES[kind]
  if (status === 'fault' || status === 'error') {
    node.attr('body/stroke', '#F56C6C')
    node.attr('body/strokeWidth', 4)
  } else if (status === 'busy' || status === 'active' || status === 'testing') {
    node.attr('body/stroke', style.stroke)
    node.attr('body/strokeWidth', 3)
    node.attr('body/strokeDasharray', '6 3')
  } else {
    node.attr('body/stroke', style.stroke)
    node.attr('body/strokeWidth', 2)
    node.attr('body/strokeDasharray', '')
  }
}

watch(
  () => topologyStore.linkStatus,
  (s) => {
    for (const key of Object.keys(s)) {
      const st = s[key] as { link_id: string; active: boolean; status?: string }
      applyLinkRuntimeState(st.link_id, st.status ?? (st.active ? 'active' : 'idle'), st.active)
    }
  },
  { deep: true },
)

watch(
  () => topologyStore.instrumentStatus,
  (s) => {
    for (const key of Object.keys(s)) {
      const st = s[key] as { instrument_id: string; status: string }
      applyNodeRuntimeState(st.instrument_id, 'instrument', st.status)
    }
  },
  { deep: true },
)

watch(
  () => topologyStore.fixtureStatus,
  (s) => {
    for (const key of Object.keys(s)) {
      const st = s[key] as { fixture_id: string; status: string }
      applyNodeRuntimeState(st.fixture_id, 'fixture', st.status)
    }
  },
  { deep: true },
)

// 故障定位高亮（§8.3.7）
function highlightFaultLocation(suspectLinks: string[] | undefined) {
  const g = graph.value
  if (!g) return
  for (const linkId of suspectLinks ?? []) {
    const edge = g.getCellById(linkId)
    if (edge && edge.isEdge()) {
      edge.attr('line/stroke', '#F56C6C')
      edge.attr('line/strokeWidth', 4)
    }
  }
}

watch(
  () => topologyStore.faults,
  (faults) => {
    for (const f of faults) {
      const loc = (f as unknown as { location?: { suspect_links?: string[] } }).location
      if (loc) highlightFaultLocation(loc.suspect_links)
    }
  },
  { deep: true },
)

// ─── 设备库：添加元素 ─────────────────────────────────────────────────────

function addEntityToCanvas(kind: string) {
  const g = graph.value
  if (!g) return
  const data = currentData.value
  if (!data) {
    ElMessage.warning('请先新建或打开一个工装配置')
    return
  }
  const id = `${kind}-${Date.now().toString(36)}`
  const pos = { x: 80 + Math.random() * 200, y: 80 + Math.random() * 200 }
  if (kind === 'psu' || kind === 'dmm' || kind === 'eload') {
    data.instruments.push({
      id,
      name: `${kind.toUpperCase()}_${data.instruments.length + 1}`,
      type: kind === 'psu' ? 'psu' : kind === 'dmm' ? 'dmm' : 'eload',
      channels: [{ id: 'CH1', name: 'CH1', type: kind === 'psu' ? 'voltage' : 'resistance', direction: 'output' }],
      position: pos,
    })
  } else if (kind === 'fixture') {
    data.fixtures.push({
      id,
      name: `FIX_${data.fixtures.length + 1}`,
      terminals: [{ id: 'T1', name: 'T1' }],
      relays: [],
      sensors: [],
      actuators: [],
      dut_slot_count: 1,
      position: pos,
    })
  } else if (kind === 'dut') {
    data.duts.push({
      id,
      product_model: `DUT_${data.duts.length + 1}`,
      test_points: [{ id: 'TP1', net: 'TP1', type: 'voltage' }],
      position: pos,
    })
  }
  renderTopology(data)
}

// ─── 加载列表/执行 ────────────────────────────────────────────────────────

async function openListDialog() {
  listDialogVisible.value = true
  loading.value = true
  try {
    const res = await listFixtureTopologies()
    topologies.value = res.items
  } catch (e) {
    ElMessage.error(`加载工装列表失败: ${e instanceof Error ? e.message : String(e)}`)
  } finally {
    loading.value = false
  }
}

async function loadTopology(id: string) {
  loading.value = true
  try {
    const res = await getFixtureTopology(id)
    current.value = res
    currentData.value = JSON.parse(JSON.stringify(res.topology_data))
    // 注册节点选择
    nextTick(() => {
      const g = graph.value
      if (!g) return
      g.on('node:click', ({ node }) => onNodeSelected(node.id))
      g.on('blank:click', () => {
        selectedInfo.value = { kind: 'none', id: '', name: '', detail: '' }
      })
      // 右键链路 → 故障注入菜单（T30，§8.3）
      g.on('edge:contextmenu', ({ e, edge }) => {
        const d = (edge.getData() ?? {}) as { kind?: string }
        if (d.kind !== 'link') return
        e.preventDefault()
        faultMenu.value = { visible: true, x: e.clientX, y: e.clientY, linkId: edge.id }
      })
      renderTopology(currentData.value!)
    })
    ElMessage.success(`已加载「${res.name}」v${res.version}`)
  } catch (e) {
    ElMessage.error(`加载失败: ${e instanceof Error ? e.message : String(e)}`)
  } finally {
    loading.value = false
    listDialogVisible.value = false
  }
}

// 从 X6 回写 position 到 topology_data
function syncPositions() {
  const g = graph.value
  const data = currentData.value
  if (!g || !data) return
  for (const node of g.getNodes()) {
    const d = (node.getData() ?? {}) as { kind?: string; entityId?: string }
    const p = node.position()
    if (d.kind === 'instrument') {
      const e = data.instruments.find((i) => i.id === d.entityId)
      if (e) e.position = { x: p.x, y: p.y }
    } else if (d.kind === 'fixture') {
      const e = data.fixtures.find((i) => i.id === d.entityId)
      if (e) e.position = { x: p.x, y: p.y }
    } else if (d.kind === 'dut') {
      const e = data.duts.find((i) => i.id === d.entityId)
      if (e) e.position = { x: p.x, y: p.y }
    }
  }
}

async function saveTopology() {
  const data = currentData.value
  if (!data) {
    ElMessage.warning('没有可保存的工装配置')
    return
  }
  syncPositions()
  loading.value = true
  try {
    if (current.value) {
      const res = await updateFixtureTopology(current.value.id, { topology_data: data })
      current.value = res
      ElMessage.success(`已保存 v${res.version}`)
    } else {
      const res = await createFixtureTopology({
        name: createForm.value.name || '未命名工装',
        version: createForm.value.version,
        product_model: createForm.value.product_model || null,
        description: createForm.value.description || null,
        topology_data: data,
      })
      current.value = res
      ElMessage.success(`已创建 v${res.version}`)
    }
  } catch (e) {
    ElMessage.error(`保存失败: ${e instanceof Error ? e.message : String(e)}`)
  } finally {
    loading.value = false
  }
}

async function runValidation() {
  if (!current.value) {
    ElMessage.warning('请先保存后再校验')
    return
  }
  validating.value = true
  try {
    validation.value = await validateFixtureTopology(current.value.id)
    if (validation.value.valid) {
      ElMessage.success(`校验通过：${validation.value.summary ?? '无错误无警告'}`)
    } else {
      ElMessage.warning(validation.value.summary ?? '校验发现错误')
    }
  } catch (e) {
    ElMessage.error(`校验失败: ${e instanceof Error ? e.message : String(e)}`)
  } finally {
    validating.value = false
  }
}

async function doExport(format: 'json' | 'yaml') {
  if (!current.value) return
  try {
    const res = await exportFixtureTopology(current.value.id, format)
    downloadFixtureExport(res.content, format, current.value.name)
  } catch (e) {
    ElMessage.error(`导出失败: ${e instanceof Error ? e.message : String(e)}`)
  }
}

async function doDuplicate() {
  if (!current.value) return
  try {
    const dup = await duplicateFixtureTopology(current.value.id)
    ElMessage.success(`已复制为「${dup.name}」v${dup.version}`)
    await openListDialog()
  } catch (e) {
    ElMessage.error(`复制失败: ${e instanceof Error ? e.message : String(e)}`)
  }
}

async function loadExecutions() {
  try {
    const res = await listExecutions(0, 20)
    executions.value = res.items
  } catch {
    executions.value = []
  }
}

function connectRuntime() {
  if (!selectedRunId.value) {
    ElMessage.warning('请选择一个执行')
    return
  }
  topologyStore.clearRuntime()
  clearRouteHighlightOnRunStart()
  validation.value = null
  topologyStore.connect(selectedRunId.value)
  runtimeConnected.value = true
  ElMessage.success(`已订阅执行 ${selectedRunId.value.slice(0, 8)} 的拓扑状态流`)
}

function disconnectRuntime() {
  topologyStore.disconnect()
  runtimeConnected.value = false
}

// 链路故障注入（T30）：右键链路弹出菜单，转发云端虚拟驱动（§8.3）
const { injecting, injectFault } = useFaultInjection()
const faultMenu = ref({ visible: false, x: 0, y: 0, linkId: '' })
const faultMenuEnabled = computed(() => Boolean(selectedRunId.value && runtimeConnected.value))

function closeFaultMenu() {
  faultMenu.value.visible = false
}

async function onFaultSelect(faultType: LinkFaultType) {
  const runId = selectedRunId.value
  const linkId = faultMenu.value.linkId
  if (!runId || !linkId) {
    ElMessage.warning('请先选择一个执行并连接运行时')
    return
  }
  const ok = await injectFault(runId, linkId, faultType)
  if (ok) {
    // 即时视觉确认（§8.3.7 fault-red）；随后以 SSE runtime 事件为准
    applyLinkRuntimeState(linkId, 'fault', false)
  }
}

// 新建空拓扑
function openCreateDialog() {
  createForm.value = { name: '', version: '1.0', product_model: '', description: '' }
  createDialogVisible.value = true
}

function createNewTopology() {
  current.value = null
  currentData.value = {
    instruments: [],
    fixtures: [],
    duts: [],
    links: [],
    routes: [],
  }
  createDialogVisible.value = false
  validation.value = null
  topologyStore.setTopology(currentData.value)
  nextTick(() => renderTopology(currentData.value!))
  ElMessage.success('已创建空白拓扑，从设备库添加元素')
}

// 版本历史
const versionDialogVisible = ref(false)
const versions = ref<Awaited<ReturnType<typeof listFixtureVersions>>>([])

async function showVersions() {
  if (!current.value) return
  versionDialogVisible.value = true
  try {
    versions.value = await listFixtureVersions(current.value.id)
  } catch (e) {
    ElMessage.error(`加载版本历史失败: ${e instanceof Error ? e.message : String(e)}`)
  }
}

onMounted(async () => {
  loadExecutions()
  try {
    templates.value = await listDeviceTemplates()
  } catch {
    templates.value = []
  }
})

// 运行时连接状态
watch(
  () => topologyStore.connected,
  (c) => {
    runtimeConnected.value = c
  },
)
</script>

<template>
  <div class="fixture-designer">
    <!-- 工具栏 -->
    <div class="toolbar">
      <el-button size="small" type="primary" @click="openCreateDialog">新建</el-button>
      <el-button size="small" @click="openListDialog">打开</el-button>
      <el-button size="small" @click="saveTopology" :loading="loading">保存</el-button>
      <el-button size="small" type="warning" @click="runValidation" :loading="validating">校验</el-button>
      <el-divider direction="vertical" />
      <el-dropdown size="small" @command="onLayoutCommand">
        <el-button size="small">布局<el-icon class="el-icon--right"><arrow-down /></el-icon></el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="auto">自动布局（分层）</el-dropdown-item>
            <el-dropdown-item command="grid">网格布局</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <el-button size="small" @click="showVersions">版本</el-button>
      <el-button size="small" @click="doDuplicate">复制</el-button>
      <el-dropdown size="small" @command="(c: string) => doExport(c as 'json' | 'yaml')">
        <el-button size="small">导出<el-icon class="el-icon--right"><arrow-down /></el-icon></el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="json">导出 JSON</el-dropdown-item>
            <el-dropdown-item command="yaml">导出 YAML</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <el-divider direction="vertical" />
      <div class="run-box">
        <el-select v-model="selectedRunId" placeholder="选择执行" size="small" style="width: 220px" clearable filterable>
          <el-option
            v-for="ex in executions"
            :key="ex.id"
            :label="`${ex.id.slice(0, 8)} · ${ex.sequence_id ?? ''} · ${ex.status}`"
            :value="ex.id"
          />
        </el-select>
        <el-button v-if="!runtimeConnected" size="small" type="success" @click="connectRuntime">运行</el-button>
        <el-button v-else size="small" type="danger" @click="disconnectRuntime">断开</el-button>
      </div>
      <el-tag v-if="runtimeConnected" type="success" size="small" class="live-badge">LIVE</el-tag>
    </div>

    <div class="workspace">
      <!-- 设备库 -->
      <aside class="library">
        <h3>设备库</h3>
        <div v-for="tpl in library" :key="tpl.kind" class="lib-item" @click="addEntityToCanvas(tpl.kind)">
          <span class="lib-icon">{{ tpl.icon }}</span>
          <span>{{ tpl.label }}</span>
        </div>
        <template v-if="templates.length">
          <h4>设备模板库</h4>
          <div v-for="tpl in templates" :key="tpl.id" class="lib-item tpl" @click="addEntityToCanvas(tpl.type)">
            <span>{{ tpl.icon || '🔧' }} {{ tpl.model }}</span>
          </div>
        </template>
      </aside>

      <!-- 拓扑画布 -->
      <main class="canvas-wrap">
        <div id="fixture-canvas" class="fixture-canvas"></div>
        <div class="canvas-hint">Shift+拖拽平移 · Ctrl+滚轮缩放 · 点击节点查看属性</div>
      </main>

      <!-- 属性面板 -->
      <aside class="property-panel">
        <h3>属性配置</h3>
        <el-empty v-if="selectedInfo.kind === 'none'" description="未选中元素" :image-size="60" />
        <template v-else>
          <el-descriptions :column="1" size="small" border>
            <el-descriptions-item label="类型">
              <el-tag size="small" :type="selectedInfo.kind === 'link' ? 'info' : 'primary'">
                {{ selectedInfo.kind === 'link' ? '链路' : selectedInfo.kind === 'instrument' ? '仪器' : selectedInfo.kind === 'fixture' ? '夹具' : 'DUT' }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="ID">{{ selectedInfo.id }}</el-descriptions-item>
            <el-descriptions-item label="名称">{{ selectedInfo.name }}</el-descriptions-item>
            <el-descriptions-item label="详情">{{ selectedInfo.detail }}</el-descriptions-item>
          </el-descriptions>
        </template>

        <h4>继电器矩阵</h4>
        <RelayMatrixEditor :fixture="selectedFixture" @relays-change="onRelaysChange" />

        <h4>信号路由</h4>
        <RouteManagementPanel
          :routes="currentData?.routes ?? []"
          :links="currentData?.links ?? []"
          :relays="allRelays"
          @routes-change="onRoutesChange"
        />

        <h4>校验结果</h4>
        <el-empty v-if="!validation" description="尚未校验" :image-size="50" />
        <template v-else>
          <el-alert
            :type="validation.valid ? 'success' : 'error'"
            :title="validation.summary ?? (validation.valid ? '校验通过' : '校验失败')"
            :closable="false"
            show-icon
            style="margin-bottom: 8px"
          />
          <div v-for="(err, i) in validation.errors" :key="`e${i}`" class="issue error">
            <el-tag size="small" type="danger">error</el-tag>
            <span>{{ err.message }}</span>
          </div>
          <div v-for="(warn, i) in validation.warnings" :key="`w${i}`" class="issue warn">
            <el-tag size="small" type="warning">warning</el-tag>
            <span>{{ warn.message }}</span>
          </div>
        </template>

        <h4>故障</h4>
        <el-empty v-if="topologyStore.faults.length === 0" description="无故障" :image-size="50" />
        <div v-for="(f, i) in topologyStore.faults" :key="i" class="issue error">
          <el-tag size="small" :type="f.severity === 'critical' ? 'danger' : f.severity === 'warning' ? 'warning' : 'danger'">
            {{ f.type }}
          </el-tag>
          <span>{{ f.message }}</span>
        </div>
      </aside>
    </div>

    <!-- 打开列表 -->
    <el-dialog v-model="listDialogVisible" title="打开工装配置" width="720px">
      <el-table :data="topologies" v-loading="loading" size="small">
        <el-table-column prop="name" label="名称" />
        <el-table-column prop="version" label="版本" width="80" />
        <el-table-column prop="product_model" label="产品" width="140" />
        <el-table-column label="更新时间" width="170">
          <template #default="{ row }">{{ new Date(row.updated_at).toLocaleString() }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90">
          <template #default="{ row }">
            <el-button size="small" type="primary" link @click="loadTopology(row.id)">打开</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>

    <!-- 新建 -->
    <el-dialog v-model="createDialogVisible" title="新建工装配置" width="480px">
      <el-form label-width="90px" size="default">
        <el-form-item label="名称" required>
          <el-input v-model="createForm.name" placeholder="如 12V/5A 电源工装" />
        </el-form-item>
        <el-form-item label="版本">
          <el-input v-model="createForm.version" placeholder="1.0" />
        </el-form-item>
        <el-form-item label="产品型号">
          <el-input v-model="createForm.product_model" placeholder="适配产品" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="createForm.description" type="textarea" :rows="2" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="createNewTopology">创建空白拓扑</el-button>
      </template>
    </el-dialog>

    <!-- 版本历史 -->
    <el-dialog v-model="versionDialogVisible" title="版本历史" width="640px">
      <el-table :data="versions" size="small">
        <el-table-column prop="version" label="版本" width="90" />
        <el-table-column prop="change_log" label="变更说明" />
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ new Date(row.created_at).toLocaleString() }}</template>
        </el-table-column>
      </el-table>
    </el-dialog>

    <!-- 链路故障注入右键菜单（T30） -->
    <LinkFaultContextMenu
      :visible="faultMenu.visible"
      :x="faultMenu.x"
      :y="faultMenu.y"
      :link-id="faultMenu.linkId"
      :disabled="!faultMenuEnabled || injecting"
      @select="onFaultSelect"
      @close="closeFaultMenu"
    />
  </div>
</template>

<style scoped>
.fixture-designer {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 60px);
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  flex-wrap: wrap;
}
.run-box {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
}
.live-badge {
  margin-left: 4px;
}
.workspace {
  flex: 1;
  display: flex;
  min-height: 0;
}
.library,
.property-panel {
  width: 230px;
  border-right: 1px solid var(--el-border-color-lighter);
  padding: 12px;
  overflow-y: auto;
  flex-shrink: 0;
}
.property-panel {
  border-right: none;
  border-left: 1px solid var(--el-border-color-lighter);
}
.library h3,
.property-panel h3 {
  margin: 0 0 10px;
  font-size: 14px;
}
.library h4,
.property-panel h4 {
  margin: 14px 0 8px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.lib-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  margin-bottom: 6px;
  border: 1px dashed var(--el-border-color);
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  transition: all 0.2s;
}
.lib-item:hover {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}
.lib-item.tpl {
  border-style: solid;
}
.lib-icon {
  font-size: 16px;
}
.canvas-wrap {
  flex: 1;
  position: relative;
  min-width: 0;
}
.fixture-canvas {
  position: absolute;
  inset: 0;
}
.canvas-hint {
  position: absolute;
  bottom: 8px;
  left: 50%;
  transform: translateX(-50%);
  font-size: 12px;
  color: var(--el-text-color-secondary);
  background: var(--el-bg-color);
  padding: 2px 10px;
  border-radius: 10px;
  opacity: 0.8;
}
.issue {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  font-size: 12px;
  margin-bottom: 4px;
  line-height: 1.4;
}
.issue.error span {
  color: var(--el-color-danger);
}
.issue.warn span {
  color: var(--el-color-warning);
}
</style>
