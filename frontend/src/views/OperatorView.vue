<script setup lang="ts">
/**
 * Operator View — 操作员控制台（T42，v41-gap-analysis #42，§10.5）。
 *
 * Route: /#/operator/:station_id（独立页面，无侧边栏布局）
 *
 * 面板构成：
 *   1. 运行状态卡 —— 自动发现 RUNNING 运行（3s 轮询 /executions 列表），
 *      支持手动输入运行 ID；展示状态/DUT 序列号/开始时间。
 *   2. 待确认检查点 —— 轮询 GET /executions/{run}/checkpoint/pending，
 *      按 created_at 升序引导处理；「确认」弹窗收集操作员姓名 + 可选备注，
 *      提交 POST .../checkpoint/ack（服务端强制门控）。
 *   3. 已完成历史 —— 本会话已确认检查点（签署人/时间/备注）。
 *   4. 完成签名 —— 运行进入终态且无待确认检查点时签收。
 *
 * 只读约束：不暴露任何管理动作（无中止/删除/编辑入口）；
 * 检查点未确认时显示开始受阻原因（canStartRun 门控，服务端同样强制）。
 * 离线提示为显示型（§10.5）：连续轮询失败 ≥3 次显示横幅。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import {
  ElAlert,
  ElButton,
  ElCard,
  ElDialog,
  ElDescriptions,
  ElDescriptionsItem,
  ElEmpty,
  ElInput,
  ElMessage,
  ElTag,
} from 'element-plus'
import {
  acknowledgeCheckpoint,
  buildAckPayload,
  buildCompletionSignature,
  canStartRun,
  isTerminalStatus,
  needsDutSwap,
  partitionCheckpoints,
  shouldShowOfflineBanner,
  sortCheckpoints,
  upsertPending,
  type CheckpointItem,
  type CompletionSignature,
  type PendingCheckpointResponse,
} from '@/utils/checkpointFlow'
import {
  acknowledgeRunCheckpoint,
  fetchPendingCheckpoint,
  listExecutions,
  type ExecutionListItem,
} from '@/api/executions'

// ─── Route param ─────────────────────────────────────────────────────────────

const route = useRoute()

const stationId = computed(() => {
  const param = route.params.station_id
  if (Array.isArray(param)) return param[0] ?? ''
  return param ?? ''
})

// ─── 运行状态 ────────────────────────────────────────────────────────────────

const runs = ref<ExecutionListItem[]>([])
const manualRunId = ref('')
const activeRunId = ref<string | null>(null)
const lastRunId = ref<string | null>(null)

const OFFLINE_FAILURE_THRESHOLD = 3
const consecutiveFailures = ref(0)
const offline = computed(() =>
  shouldShowOfflineBanner(consecutiveFailures.value, OFFLINE_FAILURE_THRESHOLD),
)

const activeRun = computed<ExecutionListItem | null>(() => {
  const found = runs.value.find((r) => r.id === activeRunId.value)
  if (found) return found
  if (activeRunId.value) {
    // 手动跟踪的运行尚未出现在轮询列表中：显示占位（视为运行中）。
    return {
      id: activeRunId.value,
      sequence_id: null,
      status: 'RUNNING',
      dut_serial: null,
      product_type: null,
      started_at: null,
      completed_at: null,
      pass_rate: null,
      error: null,
    }
  }
  return null
})

const runFinished = computed(() => Boolean(activeRun.value && isTerminalStatus(activeRun.value.status)))

/** 运行结束后出现新运行 → 提示更换 DUT。 */
const dutSwapPrompt = computed(() =>
  needsDutSwap(lastRunId.value, activeRunId.value, runFinished.value || lastRunId.value !== null),
)

function pickActiveRun(): void {
  const running = runs.value.find((r) => r.status === 'RUNNING')
  if (running && running.id !== activeRunId.value) {
    if (activeRunId.value) lastRunId.value = activeRunId.value
    activeRunId.value = running.id
    return
  }
  // 无 RUNNING：保留当前选择直到其进入终态后清空（触发换件提示）。
  if (!running && activeRun.value && isTerminalStatus(activeRun.value.status)) {
    lastRunId.value = activeRunId.value
    activeRunId.value = null
  }
}

function applyManualRunId(): void {
  const id = manualRunId.value.trim()
  if (!id) return
  if (activeRunId.value) lastRunId.value = activeRunId.value
  activeRunId.value = id
  manualRunId.value = ''
}

async function pollRuns(): Promise<void> {
  try {
    const resp = await listExecutions(0, 10)
    runs.value = resp.items
    consecutiveFailures.value = 0
    pickActiveRun()
  } catch {
    consecutiveFailures.value += 1
  }
}

// ─── 检查点流程 ──────────────────────────────────────────────────────────────

const checkpoints = ref<CheckpointItem[]>([])
const signature = ref<CompletionSignature | null>(null)

const sortedCheckpoints = computed(() => sortCheckpoints(checkpoints.value))
const pendingItems = computed(() => partitionCheckpoints(sortedCheckpoints.value).pending)
const completedItems = computed(() => partitionCheckpoints(sortedCheckpoints.value).completed)

/** 门控：存在未确认检查点时禁止开始/继续运行（展示原因）。 */
const startGate = computed(() => canStartRun(pendingItems.value))

/** 完成签名可用：运行终态 + 无待确认检查点。 */
const canSign = computed(() => runFinished.value && pendingItems.value.length === 0)

async function pollPending(): Promise<void> {
  if (!activeRunId.value) return
  let payload: PendingCheckpointResponse
  try {
    payload = await fetchPendingCheckpoint(activeRunId.value)
    consecutiveFailures.value = 0
  } catch {
    consecutiveFailures.value += 1
    return
  }
  checkpoints.value = upsertPending(checkpoints.value, payload)
}

// ─── 确认弹窗 ────────────────────────────────────────────────────────────────

const dialogVisible = ref(false)
const dialogItem = ref<CheckpointItem | null>(null)
const operatorName = ref('')
const operatorNote = ref('')
const submitting = ref(false)

function openAckDialog(item: CheckpointItem): void {
  dialogItem.value = item
  operatorNote.value = ''
  dialogVisible.value = true
}

async function confirmAck(): Promise<void> {
  const item = dialogItem.value
  if (!item || !activeRunId.value) return
  const built = buildAckPayload(item, operatorName.value, operatorNote.value)
  if (!built.ok) {
    ElMessage.warning(built.error)
    return
  }
  submitting.value = true
  try {
    const resp = await acknowledgeRunCheckpoint(activeRunId.value, built.payload)
    checkpoints.value = checkpoints.value.map((it) =>
      it.stepId === item.stepId
        ? acknowledgeCheckpoint(it, resp.operator, resp.note ?? undefined, resp.acknowledged_at)
        : it,
    )
    dialogVisible.value = false
    dialogItem.value = null
    ElMessage.success(`检查点已确认：${resp.operator}`)
  } catch {
    ElMessage.error('确认提交失败，请重试')
  } finally {
    submitting.value = false
  }
}

// ─── 完成签名 ────────────────────────────────────────────────────────────────

const signOperator = ref('')

function signOff(): void {
  if (!activeRunId.value) return
  const built = buildCompletionSignature(activeRunId.value, signOperator.value)
  if (!built.ok) {
    ElMessage.warning(built.error)
    return
  }
  signature.value = built.payload
  ElMessage.success(`运行已完成并签名：${built.payload.operator}`)
}

// ─── 展示辅助 ────────────────────────────────────────────────────────────────

const TYPE_LABELS: Record<string, string> = {
  scan: '扫码',
  manual_input: '手动输入',
  visual_check: '目视检查',
  confirm: '确认',
}

function typeLabel(type: string): string {
  return TYPE_LABELS[type] ?? type
}

function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const t = Date.parse(iso)
  return Number.isNaN(t) ? iso : new Date(t).toLocaleString()
}

// ─── 生命周期 ────────────────────────────────────────────────────────────────

let runsTimer: ReturnType<typeof setInterval> | null = null
let pendingTimer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  void pollRuns()
  runsTimer = setInterval(() => void pollRuns(), 3000)
  pendingTimer = setInterval(() => void pollPending(), 2000)
})

onBeforeUnmount(() => {
  if (runsTimer !== null) clearInterval(runsTimer)
  if (pendingTimer !== null) clearInterval(pendingTimer)
})

// 测试钩子：暴露内部状态供组件测试驱动（defineExpose 自动解包 ref）。
defineExpose({
  checkpoints,
  runs,
  activeRunId,
  lastRunId,
  consecutiveFailures,
  signature,
  openAckDialog,
  applyManualRunId,
  pollRuns,
  pollPending,
})
</script>

<template>
  <div class="operator-view" data-testid="operator-view">
    <header class="ov-header">
      <h1 class="ov-title">操作员工作站</h1>
      <el-tag data-testid="station-tag" size="large" type="info">{{ stationId || '未指定工位' }}</el-tag>
    </header>

    <el-alert
      v-if="offline"
      class="ov-banner"
      data-testid="offline-banner"
      title="网络连接中断"
      description="与服务器失去联系，数据可能不是最新。恢复后将自动刷新。"
      type="error"
      show-icon
      :closable="false"
    />

    <el-alert
      v-if="dutSwapPrompt"
      class="ov-banner"
      data-testid="dut-swap-banner"
      title="请更换被测件 (DUT)"
      description="上一发运行已结束，取下成品并放置新 DUT 后继续。"
      type="warning"
      show-icon
      :closable="false"
    />

    <!-- 运行状态卡 -->
    <el-card class="ov-card" shadow="never" data-testid="run-status-card">
      <template #header>
        <span class="ov-card-title">当前运行</span>
      </template>
      <div class="ov-run-row">
        <el-input
          v-model="manualRunId"
          class="ov-run-input"
          placeholder="手动输入运行 ID（可选）"
          clearable
          @keyup.enter="applyManualRunId"
        />
        <el-button data-testid="apply-run-btn" @click="applyManualRunId">跟踪该运行</el-button>
      </div>
      <el-empty
        v-if="!activeRun"
        data-testid="run-empty"
        description="暂无活动运行 — 等待调度或手动输入运行 ID"
      />
      <el-descriptions v-else :column="3" border>
        <el-descriptions-item label="运行 ID">
          <span data-testid="run-id">{{ activeRun.id }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="状态">
          <el-tag
            data-testid="run-status"
            :type="isTerminalStatus(activeRun.status) ? 'info' : 'success'"
          >
            {{ activeRun.status }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="DUT 序列号">{{ activeRun.dut_serial ?? '—' }}</el-descriptions-item>
        <el-descriptions-item label="开始时间">{{ formatTime(activeRun.started_at) }}</el-descriptions-item>
        <el-descriptions-item label="结束时间">{{ formatTime(activeRun.completed_at) }}</el-descriptions-item>
        <el-descriptions-item label="产品型号">{{ activeRun.product_type ?? '—' }}</el-descriptions-item>
      </el-descriptions>
      <el-alert
        v-if="!startGate.allowed"
        class="ov-gate"
        data-testid="start-gate"
        :title="'无法开始运行：' + startGate.reason"
        type="warning"
        show-icon
        :closable="false"
      />
    </el-card>

    <!-- 待确认检查点 -->
    <el-card class="ov-card" shadow="never" data-testid="pending-card">
      <template #header>
        <span class="ov-card-title">待确认检查点</span>
        <el-tag v-if="pendingItems.length" data-testid="pending-count" type="danger" size="small">
          {{ pendingItems.length }}
        </el-tag>
      </template>
      <el-empty v-if="pendingItems.length === 0" data-testid="pending-empty" description="暂无待确认检查点" />
      <ul v-else class="ov-check-list" data-testid="pending-list">
        <li v-for="item in pendingItems" :key="item.stepId" class="ov-check-item" data-testid="pending-item">
          <div class="ov-check-main">
            <el-tag size="small">{{ typeLabel(item.checkpoint.type) }}</el-tag>
            <span class="ov-check-prompt">{{ item.checkpoint.prompt }}</span>
          </div>
          <div class="ov-check-meta">创建于 {{ formatTime(item.createdAt) }} · {{ item.stepId }}</div>
          <el-button
            type="primary"
            data-testid="ack-btn"
            :disabled="submitting"
            @click="openAckDialog(item)"
          >
            确认
          </el-button>
        </li>
      </ul>
    </el-card>

    <!-- 已完成历史 -->
    <el-card class="ov-card" shadow="never" data-testid="history-card">
      <template #header>
        <span class="ov-card-title">已完成检查点</span>
      </template>
      <el-empty v-if="completedItems.length === 0" data-testid="history-empty" description="本会话尚无已确认检查点" />
      <ul v-else class="ov-check-list" data-testid="history-list">
        <li v-for="item in completedItems" :key="item.stepId" class="ov-check-item done" data-testid="history-item">
          <div class="ov-check-main">
            <el-tag size="small" type="success">✓ {{ typeLabel(item.checkpoint.type) }}</el-tag>
            <span class="ov-check-prompt">{{ item.checkpoint.prompt }}</span>
          </div>
          <div class="ov-check-meta">
            签署人 {{ item.ackedBy }} · {{ formatTime(item.ackedAt) }}
            <template v-if="item.note"> · 备注：{{ item.note }}</template>
          </div>
        </li>
      </ul>
    </el-card>

    <!-- 完成签名 -->
    <el-card v-if="canSign" class="ov-card" shadow="never" data-testid="signature-card">
      <template #header>
        <span class="ov-card-title">运行完成签名</span>
      </template>
      <template v-if="signature">
        <el-alert
          data-testid="signature-done"
          :title="`已签名：${signature.operator} · ${formatTime(signature.signed_at)}`"
          type="success"
          show-icon
          :closable="false"
        />
      </template>
      <div v-else class="ov-sign-row">
        <el-input
          v-model="signOperator"
          class="ov-run-input"
          placeholder="操作员姓名"
          data-testid="sign-input"
        />
        <el-button type="primary" data-testid="sign-btn" @click="signOff">签名完成</el-button>
      </div>
    </el-card>

    <!-- 确认弹窗：操作员姓名 + 可选备注 -->
    <el-dialog
      v-model="dialogVisible"
      title="确认检查点"
      width="480px"
      data-testid="ack-dialog"
    >
      <p v-if="dialogItem" class="ov-dialog-prompt">{{ dialogItem.checkpoint.prompt }}</p>
      <el-input
        v-model="operatorName"
        placeholder="操作员姓名（必填）"
        data-testid="ack-operator-input"
      />
      <el-input
        v-model="operatorNote"
        class="ov-note-input"
        type="textarea"
        :rows="2"
        placeholder="备注（可选）"
        data-testid="ack-note-input"
      />
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button
          type="primary"
          data-testid="ack-confirm-btn"
          :loading="submitting"
          @click="confirmAck"
        >
          确认
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.operator-view {
  min-height: 100vh;
  padding: var(--spacing-lg);
  background: var(--color-bg-secondary);
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  max-width: 960px;
  margin: 0 auto;
}

.ov-header {
  display: flex;
  align-items: center;
  gap: var(--spacing-md);
}

.ov-title {
  font-size: 1.25rem;
  font-weight: 700;
  color: var(--color-text-primary);
  margin: 0;
}

.ov-banner {
  border-radius: var(--radius-lg);
}

.ov-card {
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
}

.ov-card-title {
  font-weight: 600;
  color: var(--color-text-primary);
}

.ov-run-row,
.ov-sign-row {
  display: flex;
  gap: var(--spacing-sm);
  margin-bottom: var(--spacing-md);
}

.ov-run-input {
  max-width: 360px;
}

.ov-gate {
  margin-top: var(--spacing-md);
}

.ov-check-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
}

.ov-check-item {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
  padding: var(--spacing-sm) var(--spacing-md);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-md);
  background: var(--color-bg-primary);
}

.ov-check-item.done {
  background: var(--color-bg-tertiary);
}

.ov-check-main {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
}

.ov-check-prompt {
  color: var(--color-text-primary);
}

.ov-check-meta {
  font-size: 0.8125rem;
  color: var(--color-text-tertiary);
}

.ov-check-item .el-button {
  align-self: flex-end;
}

.ov-dialog-prompt {
  margin: 0 0 var(--spacing-md);
  color: var(--color-text-primary);
  font-weight: 500;
}

.ov-note-input {
  margin-top: var(--spacing-sm);
}
</style>
