<script setup lang="ts">
/**
 * OfflineStatusIndicator — 全局离线状态指示器（T43，v41-gap-analysis #43，§10.5）。
 *
 * 挂载于 AppLayout 头部（所有应用共享），展示：
 * - 在线/离线徽标（心跳状态点 + 文案）
 * - 待上传计数 chip（>0 时显示）
 * - 缓存健康 popover（容量条 / 最老记录 / 暂停下载警告）
 * - 手动同步按钮（POST /offline/reconcile，进行中禁用）
 *
 * 数据源：SSE `offline_status` 事件（帧 0 即时快照），断流后静默降级为
 * 60s 轮询 —— 降级仅显示细微提示，绝不弹错误（ merely degraded ≠ scary）。
 * 状态折叠全部委托纯函数 utils/offlineStatus（可测、零 DOM）。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { fetchOfflineStatus, triggerReconcile } from '@/api/offline'
import {
  capacityLevel,
  formatAgeHours,
  formatBytes,
  initialOfflineStatusState,
  reduceOfflineStatusFrame,
  setStreamConnected,
  type CapacityLevel,
  type OfflineStatusState,
} from '@/utils/offlineStatus'

// ─── 常量 ───────────────────────────────────────────────────────────────────

/** SSE 断流后的兜底轮询间隔（plan：SSE-driven + 60s fallback refresh）。 */
const FALLBACK_POLL_MS = 60_000
/** SSE 事件名（offline.py OFFLINE_STATUS_EVENT）。 */
const SSE_EVENT = 'offline_status'
const SSE_URL = '/api/v1/offline/status/stream'

// ─── 状态 ───────────────────────────────────────────────────────────────────

const state = ref<OfflineStatusState>(initialOfflineStatusState())
const reconciling = ref(false)
let eventSource: EventSource | null = null
let fallbackTimer: ReturnType<typeof setInterval> | null = null

// ─── 派生 ───────────────────────────────────────────────────────────────────

const online = computed(() => state.value.status?.online ?? false)
const hasSnapshot = computed(() => state.value.status !== null)
const pendingCount = computed(() => state.value.status?.pending_upload_count ?? 0)
const cacheHealth = computed(() => state.value.status?.cache_health ?? null)

const capacityPct = computed(() => Math.min(100, Math.max(0, cacheHealth.value?.capacity_pct ?? 0)))
const level = computed<CapacityLevel>(() => capacityLevel(cacheHealth.value?.capacity_pct ?? 0))
const levelColor = computed(() =>
  level.value === 'full' ? 'var(--color-error)' : level.value === 'warn' ? 'var(--color-warning)' : 'var(--color-success)',
)

/** SSE 断流且未恢复 —— 细微提示而非错误弹窗。 */
const streamDegraded = computed(() => hasSnapshot.value && !state.value.connected)

// ─── 纯状态折叠入口（测试直接驱动） ────────────────────────────────────────

function handleFrame(frame: unknown): void {
  state.value = reduceOfflineStatusFrame(state.value, frame)
}

// ─── 数据获取 ───────────────────────────────────────────────────────────────

async function refresh(): Promise<void> {
  try {
    const snapshot = await fetchOfflineStatus()
    handleFrame(snapshot)
  } catch {
    // 快照失败保持现状（可能尚未配置离线服务）——不弹错误。
  }
}

function startFallbackPolling(): void {
  if (fallbackTimer !== null) return
  fallbackTimer = setInterval(() => {
    void refresh()
  }, FALLBACK_POLL_MS)
}

function stopFallbackPolling(): void {
  if (fallbackTimer !== null) {
    clearInterval(fallbackTimer)
    fallbackTimer = null
  }
}

function connectStream(): void {
  // jsdom / 老环境无 EventSource —— 静默跳过（useSimulationBreakpoints 先例）。
  if (typeof EventSource === 'undefined') return
  disconnectStream()
  const es = new EventSource(SSE_URL)
  es.onopen = () => {
    stopFallbackPolling()
    state.value = setStreamConnected(state.value, true)
  }
  es.onerror = () => {
    // EventSource 自动重连；期间降级为慢轮询，不打扰操作员。
    state.value = setStreamConnected(state.value, false)
    startFallbackPolling()
  }
  es.addEventListener(SSE_EVENT, (e: MessageEvent<string>) => {
    try {
      handleFrame(JSON.parse(e.data))
    } catch {
      /* malformed frame — reducer ignores */
    }
  })
  eventSource = es
}

function disconnectStream(): void {
  eventSource?.close()
  eventSource = null
  stopFallbackPolling()
}

// ─── 手动同步 ───────────────────────────────────────────────────────────────

async function reconcile(): Promise<void> {
  if (reconciling.value) return
  reconciling.value = true
  try {
    const report = await triggerReconcile()
    if (report.ok) {
      ElMessage.success(`同步完成：上传 ${report.uploaded} 条，确认 ${report.acked} 条`)
    } else {
      ElMessage.warning('同步已执行但存在冲突，请检查隔离记录')
    }
  } catch {
    ElMessage.warning('同步暂不可用（服务未就绪或离线中）')
  } finally {
    reconciling.value = false
  }
}

onMounted(() => {
  void refresh()
  connectStream()
})

onBeforeUnmount(disconnectStream)

defineExpose({ state, reconciling, handleFrame, refresh, reconcile })
</script>

<template>
  <div class="offline-status" data-testid="offline-status">
    <el-popover placement="bottom" :width="280" trigger="click" :teleported="false" popper-class="offline-status-popper">
      <template #reference>
        <button type="button" class="status-trigger" data-testid="offline-badge" :class="online ? 'is-online' : 'is-offline'">
          <span class="status-dot" aria-hidden="true"></span>
          <span class="status-label">{{ online ? '在线' : '离线' }}</span>
          <el-tag
            v-if="pendingCount > 0"
            :data-testid="undefined"
            class="pending-chip"
            size="small"
            type="warning"
            effect="light"
            round
          >
            <span data-testid="pending-chip">待上传 {{ pendingCount }}</span>
          </el-tag>
        </button>
      </template>

      <div class="cache-panel">
        <div class="panel-title">离线缓存健康</div>

        <template v-if="cacheHealth">
          <div class="panel-row">
            <span class="row-label">缓存大小</span>
            <span class="row-value">{{ formatBytes(cacheHealth.size_bytes) }}</span>
          </div>
          <div class="panel-row">
            <span class="row-label">最老记录</span>
            <span class="row-value">{{ formatAgeHours(cacheHealth.oldest_record_age_h) }}</span>
          </div>
          <div class="panel-row panel-row--stacked">
            <span class="row-label">容量使用 {{ capacityPct.toFixed(1) }}%</span>
            <el-progress
              :percentage="capacityPct"
              :stroke-width="8"
              :show-text="false"
              :color="levelColor"
              data-testid="capacity-bar"
            />
          </div>
          <el-alert
            v-if="cacheHealth.downloads_paused"
            data-testid="paused-warning"
            class="paused-warning"
            title="缓存接近上限，新资源下载已暂停"
            type="warning"
            :closable="false"
            show-icon
          />
        </template>
        <div v-else class="panel-empty">等待状态数据…</div>

        <el-button
          class="reconcile-btn"
          type="primary"
          size="small"
          plain
          :loading="reconciling"
          :disabled="reconciling"
          data-testid="reconcile-btn"
          @click="reconcile"
        >
          手动同步
        </el-button>
      </div>
    </el-popover>

    <span v-if="streamDegraded" class="stream-hint" data-testid="stream-degraded">重连中…</span>
  </div>
</template>

<style scoped>
.offline-status {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
}

.status-trigger {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border: none;
  border-radius: var(--radius-md);
  background-color: rgba(255, 255, 255, 0.12);
  color: #fff;
  font-size: 13px;
  cursor: pointer;
  transition: background-color var(--transition-fast);
}

.status-trigger:hover {
  background-color: rgba(255, 255, 255, 0.22);
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.status-trigger.is-online .status-dot {
  background-color: var(--color-success);
  box-shadow: 0 0 6px rgba(103, 194, 58, 0.8);
}

.status-trigger.is-offline .status-dot {
  background-color: var(--color-error);
  box-shadow: 0 0 6px rgba(245, 108, 108, 0.8);
}

.pending-chip {
  margin-left: 2px;
}

.stream-hint {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.65);
}

.cache-panel {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
}

.panel-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text-primary);
}

.panel-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 13px;
}

.panel-row--stacked {
  flex-direction: column;
  align-items: stretch;
  gap: 4px;
}

.row-label {
  color: var(--color-text-secondary);
}

.row-value {
  color: var(--color-text-primary);
  font-variant-numeric: tabular-nums;
}

.paused-warning {
  --el-alert-padding: 6px 10px;
}

.reconcile-btn {
  align-self: flex-end;
}
</style>
