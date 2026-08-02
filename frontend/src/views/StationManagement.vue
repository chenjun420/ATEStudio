<script setup lang="ts">
/**
 * StationManagement — table view of all registered workstations.
 *
 * Features:
 *   - el-table with columns: Worker ID, Hostname, Status, Current Task,
 *     Version, Last Heartbeat, Actions.
 *   - Status color-coded badges (green=online, red=offline, yellow=expiring).
 *   - Expandable row detail: heartbeat history (Canvas line chart),
 *     version history, config diff viewer.
 *   - Real-time refresh via polling (15s interval).
 *   - Filter by status, sort by any column.
 *   - Actions: "配置" (config dialog), "重启" (restart/sync), "同步" (sync).
 *   - Empty state when no workers.
 *
 * Route: /stations
 */
import { computed, nextTick, ref, watch } from 'vue'
import {
  ElButton,
  ElDialog,
  ElEmpty,
  ElInput,
  ElMessage,
  ElSkeleton,
  ElTable,
  ElTableColumn,
  ElTag,
  type TableColumnCtx,
} from 'element-plus'
import { useStations, type WorkerStatus } from '@/composables/useStations'
import type { WorkerInfo } from '@/api/stations'

// ─── Composable state ───────────────────────────────────────────────────────

const {
  workers,
  loading,
  error,
  lastUpdated,
  workerDetails,
  computeStatus,
  refresh,
  fetchWorkerDetail,
  syncWorker,
  restartWorker,
} = useStations()

// ─── Table state ────────────────────────────────────────────────────────────

const statusFilter = ref<WorkerStatus | ''>('')
const expandedRows = ref<string[]>([])

// ─── Config dialog state ────────────────────────────────────────────────────

const configDialogVisible = ref(false)
const configWorkerId = ref('')
const configKey = ref('')
const configValue = ref('')
const configLoading = ref(false)

// ─── Action loading state ───────────────────────────────────────────────────

const actionLoading = ref<Map<string, string>>(new Map())

// ─── Canvas refs for heartbeat charts ───────────────────────────────────────

const heartbeatCanvases = ref<Map<string, HTMLCanvasElement | null>>(new Map())

function setHeartbeatCanvas(workerId: string, el: HTMLCanvasElement | null): void {
  if (el) {
    heartbeatCanvases.value.set(workerId, el)
  } else {
    heartbeatCanvases.value.delete(workerId)
  }
  heartbeatCanvases.value = new Map(heartbeatCanvases.value)
}

// ─── Computed ───────────────────────────────────────────────────────────────

const filteredWorkers = computed<WorkerInfo[]>(() => {
  if (!statusFilter.value) return workers.value
  return workers.value.filter((w) => computeStatus(w) === statusFilter.value)
})

const onlineCount = computed(() => workers.value.filter((w) => computeStatus(w) === 'online').length)
const offlineCount = computed(() => workers.value.filter((w) => computeStatus(w) === 'offline').length)
const expiringCount = computed(() => workers.value.filter((w) => computeStatus(w) === 'expiring').length)

const lastUpdatedText = computed(() => {
  if (!lastUpdated.value) return ''
  return lastUpdated.value.toLocaleTimeString()
})

// ─── Status helpers ─────────────────────────────────────────────────────────

function statusTagType(status: WorkerStatus): 'success' | 'danger' | 'warning' {
  switch (status) {
    case 'online': return 'success'
    case 'offline': return 'danger'
    case 'expiring': return 'warning'
    default: return 'danger'
  }
}

function statusLabel(status: WorkerStatus): string {
  switch (status) {
    case 'online': return 'Online'
    case 'offline': return 'Offline'
    case 'expiring': return 'Expiring'
    default: return 'Unknown'
  }
}

function formatHeartbeat(timestamp: string | null): string {
  if (!timestamp) return '-'
  const date = new Date(timestamp)
  if (isNaN(date.getTime())) return '-'
  const now = Date.now()
  const elapsed = now - date.getTime()
  if (elapsed < 60_000) return `${Math.floor(elapsed / 1000)}s ago`
  if (elapsed < 3_600_000) return `${Math.floor(elapsed / 60_000)}m ago`
  return date.toLocaleString()
}

function formatVersion(worker: WorkerInfo): string {
  // Version is derived from capabilities or worker_id hash; display first 8 chars of worker_id as version tag
  if (worker.capabilities.length > 0) {
    return worker.capabilities[0].slice(0, 12)
  }
  return worker.worker_id.slice(0, 8)
}

function formatCurrentTask(worker: WorkerInfo): string {
  if (worker.current_tasks === 0) return 'Idle'
  return `${worker.current_tasks}/${worker.max_concurrent_tasks} running`
}

// ─── Sorting ────────────────────────────────────────────────────────────────

function sortByHeartbeat(a: WorkerInfo, b: WorkerInfo): number {
  const timeA = a.last_heartbeat ? new Date(a.last_heartbeat).getTime() : 0
  const timeB = b.last_heartbeat ? new Date(b.last_heartbeat).getTime() : 0
  return timeA - timeB
}

function sortByStatus(a: WorkerInfo, b: WorkerInfo): number {
  const statusA = computeStatus(a)
  const statusB = computeStatus(b)
  const order: Record<WorkerStatus, number> = { online: 0, expiring: 1, offline: 2 }
  return order[statusA] - order[statusB]
}

// ─── Status filter handler ──────────────────────────────────────────────────

function handleFilterChange(value: string): void {
  const filtered = value as WorkerStatus | ''
  statusFilter.value = filtered || ''
}

type FilterHandler = (value: string) => void
const filterHandler: FilterHandler = handleFilterChange

// ─── Expand row ─────────────────────────────────────────────────────────────

async function handleExpandChange(row: WorkerInfo, expanded: WorkerInfo[]): void {
  const isExpanded = expanded.some((r) => r.worker_id === row.worker_id)
  if (isExpanded) {
    await fetchWorkerDetail(row.worker_id)
    // Draw chart after data loads and DOM updates
    await nextTick()
    drawHeartbeatChart(row.worker_id)
  }
}

// ─── Canvas heartbeat chart ─────────────────────────────────────────────────

function drawHeartbeatChart(workerId: string): void {
  const canvas = heartbeatCanvases.value.get(workerId)
  if (!canvas) return
  const ctx = canvas.getContext('2d')
  if (!ctx) return

  const detail = workerDetails.value.get(workerId)
  if (!detail || !detail.history) return

  const records = detail.history.items
  if (records.length === 0) return

  const dpr = window.devicePixelRatio || 1
  const w = canvas.clientWidth
  const h = canvas.clientHeight
  canvas.width = w * dpr
  canvas.height = h * dpr
  ctx.scale(dpr, dpr)

  ctx.clearRect(0, 0, w, h)

  // Reverse records so oldest is first (left to right)
  const data = [...records].reverse()
  const padding = { top: 20, right: 20, bottom: 30, left: 40 }
  const chartW = w - padding.left - padding.right
  const chartH = h - padding.top - padding.bottom

  // Grid lines
  ctx.strokeStyle = 'rgba(0,0,0,0.06)'
  ctx.lineWidth = 1
  for (let i = 0; i <= 4; i++) {
    const y = padding.top + (chartH / 4) * i
    ctx.beginPath()
    ctx.moveTo(padding.left, y)
    ctx.lineTo(padding.left + chartW, y)
    ctx.stroke()
  }

  // Draw heartbeat intervals as a step line
  const stepX = chartW / Math.max(data.length - 1, 1)

  // Online/offline color segments
  ctx.lineWidth = 2
  ctx.lineJoin = 'round'

  data.forEach((record, i) => {
    const x = padding.left + stepX * i
    const y = padding.top + chartH / 2

    // Draw a dot colored by status
    const isOnline = record.status === 'online'
    ctx.fillStyle = isOnline ? '#10b981' : '#ef4444'
    ctx.beginPath()
    ctx.arc(x, y, 4, 0, Math.PI * 2)
    ctx.fill()

    // Draw line to next point
    if (i < data.length - 1) {
      const nextX = padding.left + stepX * (i + 1)
      ctx.strokeStyle = isOnline ? 'rgba(16,185,129,0.4)' : 'rgba(239,68,68,0.4)'
      ctx.beginPath()
      ctx.moveTo(x, y)
      ctx.lineTo(nextX, y)
      ctx.stroke()
    }
  })

  // Time axis labels (first and last)
  ctx.fillStyle = 'rgba(0,0,0,0.4)'
  ctx.font = '10px sans-serif'
  ctx.textAlign = 'left'
  const firstTime = data[0]?.recorded_at
  if (firstTime) {
    ctx.fillText(new Date(firstTime).toLocaleTimeString(), padding.left, padding.top + chartH + 18)
  }
  ctx.textAlign = 'right'
  const lastTime = data[data.length - 1]?.recorded_at
  if (lastTime) {
    ctx.fillText(new Date(lastTime).toLocaleTimeString(), padding.left + chartW, padding.top + chartH + 18)
  }

  // Title
  ctx.fillStyle = 'rgba(0,0,0,0.6)'
  ctx.font = '11px sans-serif'
  ctx.textAlign = 'center'
  ctx.fillText(`Heartbeat History (${data.length} records)`, w / 2, 14)
}

// ─── Config dialog ──────────────────────────────────────────────────────────

function openConfigDialog(worker: WorkerInfo): void {
  configWorkerId.value = worker.worker_id
  configKey.value = ''
  configValue.value = ''
  configDialogVisible.value = true
}

async function submitConfig(): Promise<void> {
  if (!configKey.value.trim()) {
    ElMessage.warning('Please enter a config key')
    return
  }
  configLoading.value = true
  try {
    const { updateWorkerConfig } = await import('@/api/stations')
    await updateWorkerConfig(configWorkerId.value, configKey.value.trim(), configValue.value)
    ElMessage.success('Config updated successfully')
    configDialogVisible.value = false
  } catch (e: unknown) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to update config')
  } finally {
    configLoading.value = false
  }
}

// ─── Actions ────────────────────────────────────────────────────────────────

function setActionLoading(workerId: string, action: string): void {
  actionLoading.value.set(workerId, action)
  actionLoading.value = new Map(actionLoading.value)
}

function clearActionLoading(workerId: string): void {
  actionLoading.value.delete(workerId)
  actionLoading.value = new Map(actionLoading.value)
}

async function handleSync(worker: WorkerInfo): Promise<void> {
  setActionLoading(worker.worker_id, 'sync')
  try {
    await syncWorker(worker.worker_id)
    ElMessage.success(`Sync triggered for ${worker.hostname}`)
  } catch (e: unknown) {
    ElMessage.error(e instanceof Error ? e.message : 'Sync failed')
  } finally {
    clearActionLoading(worker.worker_id)
  }
}

async function handleRestart(worker: WorkerInfo): Promise<void> {
  setActionLoading(worker.worker_id, 'restart')
  try {
    await restartWorker(worker.worker_id)
    ElMessage.success(`Restart triggered for ${worker.hostname}`)
  } catch (e: unknown) {
    ElMessage.error(e instanceof Error ? e.message : 'Restart failed')
  } finally {
    clearActionLoading(worker.worker_id)
  }
}

// ─── Watch for expanded row chart redraws ───────────────────────────────────

watch(workerDetails, () => {
  expandedRows.value.forEach((workerId) => {
    requestAnimationFrame(() => {
      drawHeartbeatChart(workerId)
    })
  })
}, { deep: true })

// ─── Type for table column scope ────────────────────────────────────────────

type Scope = {
  row: WorkerInfo
  $index: number
}
</script>

<template>
  <div class="station-mgmt">
    <!-- ─── Header ─── -->
    <header class="sm-header">
      <div class="sm-header-left">
        <h1 class="sm-title">Station Management</h1>
        <span v-if="lastUpdatedText" class="sm-last-updated">
          Last updated: {{ lastUpdatedText }}
        </span>
      </div>
      <div class="sm-header-right">
        <ElTag type="success" size="small" data-testid="count-online">
          {{ onlineCount }} Online
        </ElTag>
        <ElTag type="warning" size="small" data-testid="count-expiring">
          {{ expiringCount }} Expiring
        </ElTag>
        <ElTag type="danger" size="small" data-testid="count-offline">
          {{ offlineCount }} Offline
        </ElTag>
        <ElButton size="small" :loading="loading" @click="refresh" data-testid="btn-refresh">
          Refresh
        </ElButton>
      </div>
    </header>

    <!-- ─── Status filter ─── -->
    <div class="sm-filters" data-testid="status-filters">
      <ElButton
        size="small"
        :type="statusFilter === '' ? 'primary' : 'default'"
        @click="filterHandler('')"
        data-testid="filter-all"
      >
        All ({{ workers.length }})
      </ElButton>
      <ElButton
        size="small"
        :type="statusFilter === 'online' ? 'primary' : 'default'"
        @click="filterHandler('online')"
        data-testid="filter-online"
      >
        Online
      </ElButton>
      <ElButton
        size="small"
        :type="statusFilter === 'expiring' ? 'primary' : 'default'"
        @click="filterHandler('expiring')"
        data-testid="filter-expiring"
      >
        Expiring
      </ElButton>
      <ElButton
        size="small"
        :type="statusFilter === 'offline' ? 'primary' : 'default'"
        @click="filterHandler('offline')"
        data-testid="filter-offline"
      >
        Offline
      </ElButton>
    </div>

    <!-- ─── Error banner ─── -->
    <div v-if="error" class="sm-error" data-testid="error-banner">
      <ElTag type="danger" size="default">{{ error }}</ElTag>
    </div>

    <!-- ─── Loading skeleton ─── -->
    <div v-if="loading && workers.length === 0" data-testid="loading-skeleton">
      <ElSkeleton :rows="6" animated />
    </div>

    <!-- ─── Empty state ─── -->
    <div v-else-if="!loading && filteredWorkers.length === 0" data-testid="empty-state">
      <ElEmpty description="No workstations registered" />
    </div>

    <!-- ─── Workers table ─── -->
    <div v-else class="sm-table-container" data-testid="workers-table">
      <ElTable
        :data="filteredWorkers"
        row-key="worker_id"
        stripe
        :expand-row-keys="expandedRows"
        @expand-change="handleExpandChange"
        style="width: 100%"
      >
        <!-- Expand column -->
        <ElTableColumn type="expand" data-testid="col-expand">
          <template #default="{ row }: Scope">
            <div class="sm-expand-panel" :data-testid="`expand-${row.worker_id}`">
              <!-- Heartbeat history chart -->
              <div class="sm-expand-section">
                <h4 class="sm-section-title">Heartbeat History</h4>
                <div class="sm-chart-container">
                  <canvas
                    :ref="(el) => setHeartbeatCanvas(row.worker_id, el as HTMLCanvasElement | null)"
                    class="sm-canvas"
                    :data-testid="`canvas-heartbeat-${row.worker_id}`"
                  ></canvas>
                  <ElEmpty
                    v-if="!workerDetails.get(row.worker_id)?.history || workerDetails.get(row.worker_id)?.history?.items.length === 0"
                    description="No heartbeat history"
                    :image-size="40"
                    class="sm-chart-empty"
                  />
                </div>
                <div v-if="workerDetails.get(row.worker_id)?.loading" class="sm-detail-loading">
                  Loading...
                </div>
              </div>

              <!-- Version history -->
              <div class="sm-expand-section">
                <h4 class="sm-section-title">Version History</h4>
                <div class="sm-version-list" :data-testid="`version-history-${row.worker_id}`">
                  <div v-if="row.capabilities.length === 0" class="sm-no-data">
                    No version information available
                  </div>
                  <div
                    v-for="cap in row.capabilities"
                    :key="cap"
                    class="sm-version-item"
                  >
                    <ElTag size="small" type="info">{{ cap }}</ElTag>
                  </div>
                </div>
              </div>

              <!-- Config diff viewer -->
              <div class="sm-expand-section">
                <h4 class="sm-section-title">Configuration</h4>
                <div class="sm-config-view" :data-testid="`config-view-${row.worker_id}`">
                  <div class="sm-config-row">
                    <span class="sm-config-label">Worker ID:</span>
                    <span class="sm-config-value">{{ row.worker_id }}</span>
                  </div>
                  <div class="sm-config-row">
                    <span class="sm-config-label">Max Concurrent Tasks:</span>
                    <span class="sm-config-value">{{ row.max_concurrent_tasks }}</span>
                  </div>
                  <div class="sm-config-row">
                    <span class="sm-config-label">Current Tasks:</span>
                    <span class="sm-config-value">{{ row.current_tasks }}</span>
                  </div>
                  <div class="sm-config-row">
                    <span class="sm-config-label">Capabilities:</span>
                    <span class="sm-config-value">{{ row.capabilities.join(', ') || 'none' }}</span>
                  </div>
                  <div class="sm-config-row">
                    <span class="sm-config-label">Last Heartbeat:</span>
                    <span class="sm-config-value">{{ formatHeartbeat(row.last_heartbeat) }}</span>
                  </div>
                </div>
              </div>
            </div>
          </template>
        </ElTableColumn>

        <!-- Worker ID -->
        <ElTableColumn
          prop="worker_id"
          label="Worker ID"
          sortable
          width="180"
          data-testid="col-worker-id"
        >
          <template #default="{ row }: Scope">
            <span class="sm-worker-id">{{ row.worker_id }}</span>
          </template>
        </ElTableColumn>

        <!-- Hostname -->
        <ElTableColumn
          prop="hostname"
          label="Hostname"
          sortable
          width="160"
          data-testid="col-hostname"
        />

        <!-- Status -->
        <ElTableColumn
          label="Status"
          width="120"
          :sort-method="sortByStatus"
          data-testid="col-status"
        >
          <template #default="{ row }: Scope">
            <ElTag :type="statusTagType(computeStatus(row))" size="small">
              {{ statusLabel(computeStatus(row)) }}
            </ElTag>
          </template>
        </ElTableColumn>

        <!-- Current Task -->
        <ElTableColumn
          label="Current Task"
          width="140"
          data-testid="col-current-task"
        >
          <template #default="{ row }: Scope">
            <span class="sm-task-info">{{ formatCurrentTask(row) }}</span>
          </template>
        </ElTableColumn>

        <!-- Version -->
        <ElTableColumn
          label="Version"
          width="140"
          data-testid="col-version"
        >
          <template #default="{ row }: Scope">
            <span class="sm-version">{{ formatVersion(row) }}</span>
          </template>
        </ElTableColumn>

        <!-- Last Heartbeat -->
        <ElTableColumn
          label="Last Heartbeat"
          width="180"
          sortable
          :sort-method="sortByHeartbeat"
          data-testid="col-heartbeat"
        >
          <template #default="{ row }: Scope">
            <span class="sm-heartbeat">{{ formatHeartbeat(row.last_heartbeat) }}</span>
          </template>
        </ElTableColumn>

        <!-- Actions -->
        <ElTableColumn
          label="Actions"
          width="240"
          fixed="right"
          data-testid="col-actions"
        >
          <template #default="{ row }: Scope">
            <div class="sm-actions">
              <ElButton
                size="small"
                @click="openConfigDialog(row)"
                data-testid="btn-config"
              >
                配置
              </ElButton>
              <ElButton
                size="small"
                type="warning"
                :loading="actionLoading.get(row.worker_id) === 'restart'"
                @click="handleRestart(row)"
                data-testid="btn-restart"
              >
                重启
              </ElButton>
              <ElButton
                size="small"
                type="primary"
                :loading="actionLoading.get(row.worker_id) === 'sync'"
                @click="handleSync(row)"
                data-testid="btn-sync"
              >
                同步
              </ElButton>
            </div>
          </template>
        </ElTableColumn>
      </ElTable>
    </div>

    <!-- ─── Config Dialog ─── -->
    <ElDialog
      v-model="configDialogVisible"
      title="Worker Configuration"
      width="500px"
      data-testid="config-dialog"
    >
      <div class="sm-config-dialog-body">
        <div class="sm-config-row">
          <span class="sm-config-label">Worker:</span>
          <span class="sm-config-value">{{ configWorkerId }}</span>
        </div>
        <div class="sm-config-form">
          <label class="sm-form-label">Config Key</label>
          <ElInput
            v-model="configKey"
            placeholder="e.g. instrument.oscilloscope.sample_rate"
            data-testid="config-key-input"
          />
          <label class="sm-form-label">Config Value</label>
          <ElInput
            v-model="configValue"
            type="textarea"
            :rows="3"
            placeholder="Enter config value"
            data-testid="config-value-input"
          />
        </div>
      </div>
      <template #footer>
        <ElButton @click="configDialogVisible = false" data-testid="btn-config-cancel">
          Cancel
        </ElButton>
        <ElButton
          type="primary"
          :loading="configLoading"
          @click="submitConfig"
          data-testid="btn-config-submit"
        >
          Update
        </ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.station-mgmt {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  padding: var(--spacing-md) var(--spacing-lg);
  min-height: 100vh;
  background-color: var(--color-bg-secondary);
}

/* ─── Header ─── */
.sm-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--spacing-sm);
}

.sm-header-left {
  display: flex;
  align-items: baseline;
  gap: var(--spacing-sm);
}

.sm-title {
  font-size: 1.25rem;
  font-weight: 600;
  color: var(--color-text-primary);
  margin: 0;
}

.sm-last-updated {
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
}

.sm-header-right {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
  flex-wrap: wrap;
}

/* ─── Filters ─── */
.sm-filters {
  display: flex;
  gap: var(--spacing-xs);
  flex-wrap: wrap;
}

/* ─── Error ─── */
.sm-error {
  padding: var(--spacing-xs) 0;
}

/* ─── Table ─── */
.sm-table-container {
  background-color: var(--color-bg-primary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-xl);
  overflow: hidden;
}

.sm-worker-id {
  font-family: monospace;
  font-size: 0.8125rem;
  color: var(--color-text-primary);
}

.sm-task-info,
.sm-version,
.sm-heartbeat {
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
}

/* ─── Actions ─── */
.sm-actions {
  display: flex;
  gap: var(--spacing-xs);
  flex-wrap: nowrap;
}

/* ─── Expand panel ─── */
.sm-expand-panel {
  padding: var(--spacing-md) var(--spacing-lg);
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  background-color: var(--color-bg-tertiary);
}

.sm-expand-section {
  background-color: var(--color-bg-primary);
  border-radius: var(--radius-md);
  padding: var(--spacing-sm) var(--spacing-md);
}

.sm-section-title {
  font-size: 0.9375rem;
  font-weight: 600;
  color: var(--color-text-primary);
  margin: 0 0 var(--spacing-sm) 0;
}

/* ─── Chart ─── */
.sm-chart-container {
  position: relative;
  width: 100%;
  height: 180px;
}

.sm-canvas {
  width: 100%;
  height: 100%;
  display: block;
}

.sm-chart-empty {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
}

.sm-detail-loading {
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
  padding: var(--spacing-xs) 0;
}

/* ─── Version list ─── */
.sm-version-list {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-xs);
}

.sm-version-item {
  display: inline-flex;
}

.sm-no-data {
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
}

/* ─── Config view ─── */
.sm-config-view {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.sm-config-row {
  display: flex;
  gap: var(--spacing-sm);
  font-size: 0.8125rem;
}

.sm-config-label {
  font-weight: 600;
  color: var(--color-text-primary);
  min-width: 180px;
}

.sm-config-value {
  color: var(--color-text-secondary);
  flex: 1;
}

/* ─── Config dialog ─── */
.sm-config-dialog-body {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.sm-config-form {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.sm-form-label {
  font-size: 0.875rem;
  font-weight: 500;
  color: var(--color-text-primary);
}

/* ─── Responsive ─── */
@media (max-width: 768px) {
  .station-mgmt {
    padding: var(--spacing-sm);
  }

  .sm-chart-container {
    height: 140px;
  }
}
</style>
