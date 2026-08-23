<script setup lang="ts">
/**
 * SimulationConsole — 仿真调试控制台（设计文档 §8.4）。
 *
 * 功能模块：
 *   - 仿真配置面板：仿真层级（driver/dry_run/full）、噪声模型、UUT/噪声参数、随机种子
 *   - 执行控制：选择序列 → 创建执行（run_id）→ 启动仿真 → 清空结果
 *   - 故障注入面板：配置故障注入规则（§7.7.2）随 simulate 请求下发
 *   - 调用日志：simulate 返回的决策/测量事件表格，按类型筛选
 *   - 断点管理：debug breakpoints CRUD（步骤级，需 ATE_DEV_MODE）
 *   - 仿真报告：状态 / 耗时 / 统计摘要（§8.4 仿真报告）
 *
 * Route: /monitor/simulation
 */
import { computed, onMounted, ref } from 'vue'
import {
  ElButton,
  ElCard,
  ElDialog,
  ElForm,
  ElFormItem,
  ElInput,
  ElInputNumber,
  ElMessage,
  ElOption,
  ElSelect,
  ElTable,
  ElTableColumn,
  ElTag,
  ElSwitch,
} from 'element-plus'
import { Delete, VideoPlay, RefreshLeft } from '@element-plus/icons-vue'
import { fetchSequences, type Sequence } from '@/api/sequences'
import {
  createExecution,
  fetchExecutionDiff,
  listExecutions,
  type ExecutionListItem,
} from '@/api/executions'
import type { DiffSummary } from '@/utils/diffView'
import {
  runSimulation,
  type NoiseModel,
  type SimulationResponse,
  type SimulationTier,
} from '@/api/simulation'
import {
  createBreakpoint,
  deleteBreakpoint,
  listBreakpoints,
  type DebugBreakpoint,
} from '@/api/debug'
import { useTopologySimulation } from '@/composables/useTopologySimulation'
import InstrumentGantt from '@/components/InstrumentGantt.vue'
import ExecutionDiffPanel from '@/components/ExecutionDiffPanel.vue'

// ─── 仿真层级/噪声选项（与 useSimulation 对齐）──────────────────────────────

const TIERS: Array<{ value: SimulationTier; label: string; description: string }> = [
  { value: 'driver', label: '驱动级', description: '仪器驱动级仿真 - SIM 驱动器替代真实仪器' },
  { value: 'dry_run', label: 'DryRun', description: '调度器空跑 - 完整调度图遍历，无真实执行' },
  { value: 'full', label: '全链路', description: '驱动仿真 + 调度空跑 + 噪声模型' },
]

const NOISE_MODELS: Array<{ value: NoiseModel; label: string }> = [
  { value: 'GAUSSIAN', label: 'Gaussian' },
  { value: 'GAUSSIAN_DRIFT', label: 'Drift' },
  { value: 'GAUSSIAN_BIAS', label: 'Bias' },
  { value: 'FULL', label: 'Full' },
]

// ─── 状态 ──────────────────────────────────────────────────────────────────

const sequences = ref<Sequence[]>([])
const selectedSequenceId = ref('')
const runId = ref('')
const creatingExecution = ref(false)

const tier = ref<SimulationTier>('full')
const noiseModel = ref<NoiseModel>('GAUSSIAN')
const noiseSigma = ref(0.001)
const driftRate = ref(0.0)
const bias = ref(0.0)
const seed = ref<number | null>(42)

const running = ref(false)
const result = ref<SimulationResponse | null>(null)
const error = ref<string | null>(null)

const eventFilter = ref<'all' | 'decision' | 'measurement'>('all')
const ganttVisible = ref(false) // T36 仪器甘特时间线折叠开关

// T37 运行对比（基线选择 + diff 摘要）
const diffVisible = ref(false)
const baselineRunId = ref('')
const runs = ref<ExecutionListItem[]>([])
const diffSummary = ref<DiffSummary | null>(null)
const diffLoading = ref(false)
const baselineOptions = computed(() => runs.value.filter((r) => r.id !== runId.value))

// 故障注入规则
const faultRules = ref<Array<{ type: string; count?: number; probability?: number; condition?: string; action?: string }>>([])
const faultDialogVisible = ref(false)
const faultForm = ref({ type: 'network_delay', count: 1, probability: 1.0, condition: '', action: 'inject' })

// 断点
const breakpoints = ref<DebugBreakpoint[]>([])
const bpDialogVisible = ref(false)
const bpForm = ref({ step_id: '', condition: '', enabled: true })
const bpLoading = ref(false)

// 拓扑驱动仿真初始化（T31，§8.3.8）：启动前校验链路并派生 GPIB/TCP 初始化段
const { validateBeforeStart, buildInitSection } = useTopologySimulation()

// ─── 计算 ──────────────────────────────────────────────────────────────────

const noiseEnabled = computed(() => tier.value === 'full')
const filteredEvents = computed(() => {
  const events = result.value?.events ?? []
  if (eventFilter.value === 'all') return events
  return events.filter((e) => e.event_type === eventFilter.value)
})

const stats = computed(() => {
  const s = result.value?.statistics
  if (!s) return []
  return Object.entries(s).map(([key, value]) => ({
    key,
    value: typeof value === 'number' ? value.toLocaleString() : String(value),
  }))
})

const tierLabel = computed(() => TIERS.find((t) => t.value === tier.value)?.label ?? tier.value)

// ─── 执行控制 ──────────────────────────────────────────────────────────────

async function loadSequences() {
  try {
    sequences.value = await fetchSequences()
  } catch (e) {
    ElMessage.error(`加载序列失败: ${e instanceof Error ? e.message : String(e)}`)
  }
}

async function createAndStart() {
  if (!selectedSequenceId.value) {
    ElMessage.warning('请先选择测试序列')
    return
  }
  creatingExecution.value = true
  error.value = null
  try {
    const execution = await createExecution({ sequence_id: selectedSequenceId.value })
    runId.value = execution.id
    ElMessage.success(`已创建执行 ${execution.id.slice(0, 8)}，可启动仿真`)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
    ElMessage.error(`创建执行失败: ${error.value}`)
  } finally {
    creatingExecution.value = false
  }
}

async function startSimulation() {
  if (!runId.value) {
    ElMessage.warning('请先创建执行（选择序列 → 创建执行）')
    return
  }
  if (!(await validateBeforeStart())) return
  running.value = true
  error.value = null
  result.value = null
  try {
    const request: Parameters<typeof runSimulation>[1] = {
      tier: tier.value,
      noise_model: noiseModel.value,
      noise_sigma: noiseSigma.value,
      drift_rate: driftRate.value,
      bias: bias.value,
      seed: seed.value ?? undefined,
      fault_config: faultRules.value.length ? faultRules.value : undefined,
      topology_init: buildInitSection(),
    }
    const res = await runSimulation(runId.value, request)
    result.value = res
    if (res.status === 'passed') {
      ElMessage.success(`仿真完成：${res.events.length} 个事件，${res.duration_seconds.toFixed(2)}s`)
    } else {
      ElMessage.warning(`仿真状态：${res.status}，${res.events.length} 个事件`)
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
    ElMessage.error(`仿真失败: ${error.value}`)
  } finally {
    running.value = false
  }
}

function clearResult() {
  result.value = null
  error.value = null
  runId.value = ''
  selectedSequenceId.value = ''
}

// ─── 运行对比（T37）────────────────────────────────────────────────────────

async function toggleDiff() {
  diffVisible.value = !diffVisible.value
  if (diffVisible.value && runs.value.length === 0) {
    try {
      runs.value = (await listExecutions(0, 50)).items
    } catch {
      ElMessage.error('加载历史运行失败')
    }
  }
}

async function loadDiff() {
  if (!runId.value || !baselineRunId.value) {
    ElMessage.warning('请先创建执行并选择基线运行')
    return
  }
  diffLoading.value = true
  try {
    diffSummary.value = await fetchExecutionDiff(runId.value, baselineRunId.value)
  } catch (e) {
    diffSummary.value = null
    ElMessage.error(`对比失败: ${e instanceof Error ? e.message : String(e)}`)
  } finally {
    diffLoading.value = false
  }
}

// ─── 故障注入 ──────────────────────────────────────────────────────────────

function addFaultRule() {
  if (!faultForm.value.type) return
  const rule: { type: string; count?: number; probability?: number; condition?: string; action?: string } = {
    type: faultForm.value.type,
    action: faultForm.value.action || 'inject',
  }
  if (faultForm.value.count !== undefined && faultForm.value.count > 0) rule.count = faultForm.value.count
  if (faultForm.value.probability !== undefined && faultForm.value.probability < 1) rule.probability = faultForm.value.probability
  if (faultForm.value.condition) rule.condition = faultForm.value.condition
  faultRules.value.push(rule)
  faultDialogVisible.value = false
  ElMessage.success(`已添加故障注入规则：${faultForm.value.type}`)
}

function removeFaultRule(index: number) {
  faultRules.value.splice(index, 1)
}

// ─── 断点 ──────────────────────────────────────────────────────────────────

async function loadBreakpoints() {
  bpLoading.value = true
  try {
    const res = await listBreakpoints(runId.value || undefined)
    breakpoints.value = res.items
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e)
    // 403 表示未开启 dev mode——静默提示
    ElMessage.warning(`加载断点失败（可能未开启 ATE_DEV_MODE）: ${msg}`)
  } finally {
    bpLoading.value = false
  }
}

async function addBreakpoint() {
  if (!bpForm.value.step_id) {
    ElMessage.warning('请输入步骤 ID')
    return
  }
  try {
    await createBreakpoint({
      session_id: runId.value || null,
      step_id: bpForm.value.step_id,
      condition: bpForm.value.condition || null,
      enabled: bpForm.value.enabled,
    })
    bpDialogVisible.value = false
    bpForm.value = { step_id: '', condition: '', enabled: true }
    await loadBreakpoints()
    ElMessage.success('断点已创建')
  } catch (e) {
    ElMessage.error(`创建断点失败（需 ATE_DEV_MODE=true）: ${e instanceof Error ? e.message : String(e)}`)
  }
}

async function removeBreakpoint(id: string) {
  try {
    await deleteBreakpoint(id)
    await loadBreakpoints()
    ElMessage.success('断点已删除')
  } catch (e) {
    ElMessage.error(`删除断点失败: ${e instanceof Error ? e.message : String(e)}`)
  }
}

onMounted(() => {
  loadSequences()
  loadBreakpoints()
})
</script>

<template>
  <div class="simulation-console">
    <!-- 顶部执行控制 -->
    <div class="control-bar">
      <el-select v-model="selectedSequenceId" placeholder="选择测试序列" size="default" style="width: 280px" clearable filterable>
        <el-option v-for="s in sequences" :key="s.id" :label="s.name" :value="s.id" />
      </el-select>
      <el-button type="primary" :loading="creatingExecution" @click="createAndStart">创建执行</el-button>
      <el-button type="success" :loading="running" @click="startSimulation">
        <el-icon><VideoPlay /></el-icon>&nbsp;启动仿真
      </el-button>
      <el-button @click="clearResult">
        <el-icon><RefreshLeft /></el-icon>&nbsp;清空
      </el-button>
      <el-tag v-if="runId" type="info" size="default">run_id: {{ runId.slice(0, 8) }}</el-tag>
    </div>

    <div class="workspace">
      <!-- 左侧：仿真配置 + 故障注入 + 断点 -->
      <aside class="left-panel">
        <el-card shadow="never" class="panel-card">
          <template #header>仿真配置</template>
          <el-form label-width="72px" size="small">
            <el-form-item label="仿真层级">
              <el-select v-model="tier" style="width: 100%">
                <el-option v-for="t in TIERS" :key="t.value" :label="t.label" :value="t.value" />
              </el-select>
            </el-form-item>
            <el-form-item v-if="noiseEnabled" label="噪声模型">
              <el-select v-model="noiseModel" style="width: 100%">
                <el-option v-for="m in NOISE_MODELS" :key="m.value" :label="m.label" :value="m.value" />
              </el-select>
            </el-form-item>
            <el-form-item v-if="noiseEnabled" label="Sigma">
              <el-input-number v-model="noiseSigma" :step="0.001" :min="0" style="width: 100%" size="small" />
            </el-form-item>
            <el-form-item v-if="noiseEnabled" label="漂移">
              <el-input-number v-model="driftRate" :step="0.01" size="small" style="width: 100%" />
            </el-form-item>
            <el-form-item v-if="noiseEnabled" label="偏差">
              <el-input-number v-model="bias" :step="0.01" size="small" style="width: 100%" />
            </el-form-item>
            <el-form-item label="种子">
              <el-input-number v-model="seed" :step="1" size="small" style="width: 100%" />
            </el-form-item>
          </el-form>
        </el-card>

        <el-card shadow="never" class="panel-card">
          <template #header>
            <div class="card-header">
              <span>故障注入（§7.7）</span>
              <el-button size="small" type="primary" link @click="faultDialogVisible = true">+ 添加</el-button>
            </div>
          </template>
          <el-empty v-if="faultRules.length === 0" description="无故障注入规则" :image-size="40" />
          <div v-for="(rule, i) in faultRules" :key="i" class="fault-rule">
            <el-tag size="small" type="danger">{{ rule.type }}</el-tag>
            <span v-if="rule.count">×{{ rule.count }}</span>
            <span v-if="rule.probability !== undefined && rule.probability < 1">p={{ rule.probability }}</span>
            <el-button size="small" text type="danger" @click="removeFaultRule(i)">
              <el-icon><Delete /></el-icon>
            </el-button>
          </div>
        </el-card>

        <el-card shadow="never" class="panel-card">
          <template #header>
            <div class="card-header">
              <span>断点</span>
              <el-button size="small" type="primary" link @click="bpDialogVisible = true">+ 添加</el-button>
            </div>
          </template>
          <el-empty v-if="breakpoints.length === 0" description="无断点" :image-size="40" />
          <div v-for="bp in breakpoints" :key="bp.id" class="fault-rule">
            <el-tag size="small" :type="bp.enabled ? 'warning' : 'info'">{{ bp.step_id || bp.node_id || bp.id }}</el-tag>
            <el-button size="small" text type="danger" @click="removeBreakpoint(bp.id)">
              <el-icon><Delete /></el-icon>
            </el-button>
          </div>
        </el-card>
      </aside>

      <!-- 中间：调用日志 -->
      <main class="log-panel">
        <div class="log-toolbar">
          <span class="panel-title">调用日志（{{ result?.events.length ?? 0 }} 事件）</span>
          <el-radio-group v-model="eventFilter" size="small">
            <el-radio-button label="all">全部</el-radio-button>
            <el-radio-button label="decision">决策</el-radio-button>
            <el-radio-button label="measurement">测量</el-radio-button>
          </el-radio-group>
        </div>
        <el-empty v-if="!result" description="尚未运行仿真" />
        <el-table v-else :data="filteredEvents" size="small" height="100%" stripe>
          <el-table-column prop="step_id" label="步骤" width="140" />
          <el-table-column prop="event_type" label="类型" width="110">
            <template #default="{ row }">
              <el-tag size="small" :type="row.event_type === 'measurement' ? 'success' : 'info'">
                {{ row.event_type }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="详情">
            <template #default="{ row }">
              <code class="event-detail">{{ JSON.stringify(row.data) }}</code>
            </template>
          </el-table-column>
        </el-table>
        <!-- T36 仪器甘特时间线：折叠区，展开后由 result.events 派生 -->
        <div class="gantt-section">
          <el-button size="small" link type="primary" @click="ganttVisible = !ganttVisible">
            {{ ganttVisible ? '收起仪器时间线 ▲' : '展开仪器时间线 ▼' }}
          </el-button>
          <InstrumentGantt v-if="ganttVisible && result" :events="result.events" />
        </div>
        <!-- T37 运行对比：选基线 → 拉取 ExecutionDiff 摘要 -->
        <div class="gantt-section">
          <el-button size="small" link type="primary" @click="toggleDiff">
            {{ diffVisible ? '收起运行对比 ▲' : '展开运行对比 ▼' }}
          </el-button>
          <template v-if="diffVisible">
            <el-select v-model="baselineRunId" placeholder="选择基线运行" size="small" filterable clearable style="width: 220px; margin: 4px 0">
              <el-option v-for="r in baselineOptions" :key="r.id" :label="`${r.id.slice(0, 8)} · ${r.status}`" :value="r.id" />
            </el-select>
            <el-button size="small" type="primary" :loading="diffLoading" style="margin-left: 8px" @click="loadDiff">对比</el-button>
            <ExecutionDiffPanel :summary="diffSummary" :loading="diffLoading" />
          </template>
        </div>
      </main>

      <!-- 右侧：仿真报告 -->
      <aside class="right-panel">
        <el-card shadow="never" class="panel-card">
          <template #header>仿真报告</template>
          <div v-if="!result" class="report-empty">运行仿真后展示结果</div>
          <template v-else>
            <div class="report-line">
              <span>状态</span>
              <el-tag size="small" :type="result.status === 'passed' ? 'success' : result.status === 'error' ? 'danger' : 'warning'">
                {{ result.status }}
              </el-tag>
            </div>
            <div class="report-line"><span>层级</span><span>{{ tierLabel }}</span></div>
            <div class="report-line">
              <span>耗时</span><span>{{ result.duration_seconds.toFixed(3) }}s</span>
            </div>
            <el-divider style="margin: 8px 0" />
            <div v-for="(stat, i) in stats" :key="i" class="report-line stat">
              <span>{{ stat.key }}</span>
              <code class="stat-value">{{ stat.value }}</code>
            </div>
          </template>
        </el-card>
        <el-alert v-if="error" type="error" :title="error" :closable="false" style="margin-top: 8px" />
      </aside>
    </div>

    <!-- 故障注入对话框 -->
    <el-dialog v-model="faultDialogVisible" title="添加故障注入规则" width="440px">
      <el-form label-width="80px" size="default">
        <el-form-item label="类型" required>
          <el-select v-model="faultForm.type" style="width: 100%">
            <el-option label="网络延迟" value="network_delay" />
            <el-option label="丢包" value="packet_loss" />
            <el-option label="协议错误" value="protocol_error" />
            <el-option label="测量越界" value="measurement_out_of_range" />
            <el-option label="仪器断连" value="instrument_disconnect" />
            <el-option label="超时" value="timeout" />
          </el-select>
        </el-form-item>
        <el-form-item label="次数">
          <el-input-number v-model="faultForm.count" :min="1" style="width: 100%" />
        </el-form-item>
        <el-form-item label="概率">
          <el-input-number v-model="faultForm.probability" :min="0" :max="1" :step="0.1" style="width: 100%" />
        </el-form-item>
        <el-form-item label="条件">
          <el-input v-model="faultForm.condition" placeholder="可选，如 step_id == 'dmm1'" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="faultDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="addFaultRule">添加</el-button>
      </template>
    </el-dialog>

    <!-- 断点对话框 -->
    <el-dialog v-model="bpDialogVisible" title="添加断点" width="440px">
      <el-form label-width="80px" size="default">
        <el-form-item label="步骤 ID" required>
          <el-input v-model="bpForm.step_id" placeholder="如 dmm1" />
        </el-form-item>
        <el-form-item label="条件">
          <el-input v-model="bpForm.condition" placeholder="可选断点条件表达式" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="bpForm.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="bpDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="addBreakpoint">添加</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.simulation-console {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 60px);
}
.control-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  flex-wrap: wrap;
}
.workspace {
  flex: 1;
  display: flex;
  min-height: 0;
}
.left-panel,
.right-panel {
  width: 280px;
  overflow-y: auto;
  padding: 8px;
  flex-shrink: 0;
}
.right-panel {
  border-left: 1px solid var(--el-border-color-lighter);
}
.left-panel {
  border-right: 1px solid var(--el-border-color-lighter);
}
.panel-card {
  margin-bottom: 8px;
}
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.fault-rule {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  padding: 4px 0;
}
.log-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  padding: 8px 12px;
}
.gantt-section {
  border-top: 1px solid var(--el-border-color-lighter);
  margin-top: 8px;
  padding-top: 4px;
}
.log-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
.panel-title {
  font-weight: 600;
  font-size: 14px;
}
.event-detail {
  font-size: 11px;
  word-break: break-all;
  color: var(--el-text-color-secondary);
}
.report-empty {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  text-align: center;
  padding: 20px 0;
}
.report-line {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12px;
  padding: 4px 0;
}
.report-line.stat .stat-value {
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
