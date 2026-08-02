<script setup lang="ts">
/**
 * Execution History view — filterable table + detail panel.
 *
 * Features:
 *   - Filterable table: serial_number, product_type, start_time, result, pass_rate, actions.
 *   - Filters: date range picker, product type input, result status filter, serial number search.
 *   - Detail panel (slide-over): step timeline (el-timeline), measurement charts (Canvas), event log list.
 *   - Pagination (el-pagination).
 *   - Export button (triggers CSV download).
 *
 * Route: /history
 */
import { computed, nextTick, ref, watch } from 'vue'
import {
  ElCard,
  ElTable,
  ElTableColumn,
  ElTag,
  ElButton,
  ElInput,
  ElSelect,
  ElOption,
  ElDatePicker,
  ElPagination,
  ElTimeline,
  ElTimelineItem,
  ElEmpty,
  ElSkeleton,
  ElAlert,
  ElDrawer,
  ElIcon,
  ElRow,
  ElCol,
} from 'element-plus'
import { Download, Refresh, Search, Close } from '@element-plus/icons-vue'
import { useExecutionHistory } from '@/composables/useExecutionHistory'
import type { ExecutionListItem, StepResult, MeasurementData } from '@/api/executions'

// ─── Composable state ──────────────────────────────────────────────────────────

const {
  serialNumber,
  productType,
  statusFilter,
  dateRange,
  hasActiveFilters,
  currentPage,
  pageSize,
  total,
  executions,
  detail,
  loading,
  error,
  detailLoading,
  detailError,
  fetchList,
  fetchDetail,
  clearDetail,
  applyFilters,
  onPaginationChange,
  resetFilters,
  exportCsv,
} = useExecutionHistory()

// ─── Detail panel state ─────────────────────────────────────────────────────────

const drawerVisible = ref<boolean>(false)
const measurementCanvas = ref<HTMLCanvasElement | null>(null)

// ─── Status options ─────────────────────────────────────────────────────────────

const statusOptions = [
  { value: '', label: 'All Statuses' },
  { value: 'COMPLETED', label: 'Completed' },
  { value: 'FAILED', label: 'Failed' },
  { value: 'ABORTED', label: 'Aborted' },
  { value: 'RUNNING', label: 'Running' },
  { value: 'PENDING', label: 'Pending' },
]

// ─── Computed ────────────────────────────────────────────────────────────────────

/** Flatten all measurements from the detail's step_results for the chart. */
const allMeasurements = computed<MeasurementData[]>(() => {
  const steps = detail.value?.step_results ?? []
  const result: MeasurementData[] = []
  for (const step of steps) {
    if (step.measurements) {
      result.push(...step.measurements)
    }
  }
  return result
})

/** Event log derived from step_results. */
const eventLog = computed<{ step_id: string; name: string; status: string; time: string | null; error: string | null }[]>(() => {
  const steps = detail.value?.step_results ?? []
  return steps.map((s: StepResult) => ({
    step_id: s.step_id,
    name: s.name ?? s.step_id,
    status: s.status,
    time: s.completed_at ?? s.started_at,
    error: s.error ?? null,
  }))
})

// ─── Helpers ────────────────────────────────────────────────────────────────────

function statusTagType(status: string): 'success' | 'warning' | 'danger' | 'info' | 'primary' {
  switch (status) {
    case 'COMPLETED': return 'success'
    case 'RUNNING': return 'primary'
    case 'PENDING': return 'info'
    case 'FAILED': return 'danger'
    case 'ABORTED': return 'warning'
    default: return 'info'
  }
}

function outcomeTagType(outcome: string): 'success' | 'warning' | 'danger' | 'info' {
  switch (outcome) {
    case 'PASS': return 'success'
    case 'WARNING': return 'warning'
    case 'FAIL': return 'danger'
    default: return 'info'
  }
}

function formatDateTime(iso: string | null): string {
  if (!iso) return '-'
  const d = new Date(iso)
  return d.toLocaleString(undefined, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

function formatPassRate(rate: number | null): string {
  if (rate === null) return '-'
  return `${rate.toFixed(1)}%`
}

function shortId(id: string): string {
  return id.length > 8 ? id.slice(0, 8) : id
}

function timelineTagType(status: string): 'success' | 'warning' | 'danger' | 'primary' | 'info' {
  switch (status) {
    case 'COMPLETED': return 'success'
    case 'PASSED': return 'success'
    case 'SKIPPED': return 'info'
    case 'FAILED': return 'danger'
    case 'ERROR': return 'danger'
    case 'RUNNING': return 'primary'
    case 'PENDING': return 'info'
    default: return 'info'
  }
}

// ─── Row click → open detail panel ────────────────────────────────────────────────

async function openDetail(row: ExecutionListItem): Promise<void> {
  drawerVisible.value = true
  await fetchDetail(row.id)
  // Draw chart after data arrives and DOM updates
  await nextTick()
  drawMeasurementChart()
}

// ─── Canvas chart: measurement values ──────────────────────────────────────────────

function drawMeasurementChart(): void {
  const canvas = measurementCanvas.value
  if (!canvas) return
  const ctx = canvas.getContext('2d')
  if (!ctx) return

  const dpr = window.devicePixelRatio || 1
  const w = canvas.clientWidth
  const h = canvas.clientHeight
  canvas.width = w * dpr
  canvas.height = h * dpr
  ctx.scale(dpr, dpr)

  ctx.clearRect(0, 0, w, h)

  const data = allMeasurements.value
  if (data.length === 0) return

  const padding = { top: 20, right: 20, bottom: 40, left: 50 }
  const chartW = w - padding.left - padding.right
  const chartH = h - padding.top - padding.bottom

  // Extract numeric values for scaling
  const values = data.map((m) => m.value).filter((v): v is number => v !== null)
  if (values.length === 0) return

  const minVal = Math.min(...values, 0)
  const maxVal = Math.max(...values, 1)
  const range = maxVal - minVal || 1

  // Grid lines
  ctx.strokeStyle = 'rgba(0,0,0,0.06)'
  ctx.lineWidth = 1
  for (let i = 0; i <= 4; i++) {
    const y = padding.top + (chartH / 4) * i
    ctx.beginPath()
    ctx.moveTo(padding.left, y)
    ctx.lineTo(padding.left + chartW, y)
    ctx.stroke()

    // Y-axis labels
    const val = maxVal - (range / 4) * i
    ctx.fillStyle = 'rgba(0,0,0,0.4)'
    ctx.font = '10px sans-serif'
    ctx.textAlign = 'right'
    ctx.fillText(val.toFixed(2), padding.left - 6, y + 3)
  }

  // X-axis labels (measurement names, truncated)
  ctx.fillStyle = 'rgba(0,0,0,0.4)'
  ctx.font = '10px sans-serif'
  ctx.textAlign = 'center'

  const stepX = chartW / Math.max(data.length, 1)

  // Bars for each measurement
  const barWidth = stepX * 0.5
  data.forEach((m, i) => {
    const x = padding.left + stepX * i + (stepX - barWidth) / 2
    const val = m.value ?? 0
    const normalized = (val - minVal) / range
    const barH = normalized * chartH
    const y = padding.top + chartH - barH

    // Color based on outcome
    let color = '#aa3bff'
    if (m.outcome === 'PASS') color = '#10b981'
    else if (m.outcome === 'FAIL') color = '#ef4444'
    else if (m.outcome === 'WARNING') color = '#f59e0b'

    ctx.fillStyle = color
    ctx.fillRect(x, y, barWidth, barH)

    // Outcome dot
    ctx.beginPath()
    ctx.arc(x + barWidth / 2, y - 4, 3, 0, Math.PI * 2)
    ctx.fill()

    // X-axis label (truncated)
    const label = m.name.length > 10 ? m.name.slice(0, 8) + '...' : m.name
    ctx.save()
    ctx.translate(x + barWidth / 2, padding.top + chartH + 8)
    ctx.rotate(-0.5)
    ctx.fillText(label, 0, 0)
    ctx.restore()
  })

  // Axis lines
  ctx.strokeStyle = 'rgba(0,0,0,0.15)'
  ctx.lineWidth = 1
  ctx.beginPath()
  ctx.moveTo(padding.left, padding.top)
  ctx.lineTo(padding.left, padding.top + chartH)
  ctx.lineTo(padding.left + chartW, padding.top + chartH)
  ctx.stroke()
}

// ─── Watchers ────────────────────────────────────────────────────────────────────

// Redraw chart when detail data changes
watch(allMeasurements, () => {
  nextTick(() => drawMeasurementChart())
})

// ─── Table row key ──────────────────────────────────────────────────────────────────

function rowKey(row: ExecutionListItem): string {
  return row.id
}
</script>

<template>
  <div class="exec-history">
    <!-- ─── Header ─── -->
    <header class="eh-header">
      <div class="eh-header-left">
        <h1 class="eh-title">Execution History</h1>
      </div>
      <div class="eh-header-right">
        <ElButton
          size="small"
          :loading="loading"
          :icon="Refresh"
          @click="fetchList"
          data-testid="btn-refresh"
        >
          Refresh
        </ElButton>
        <ElButton
          size="small"
          :icon="Download"
          :disabled="executions.length === 0"
          @click="exportCsv"
          data-testid="btn-export"
        >
          Export CSV
        </ElButton>
      </div>
    </header>

    <!-- ─── Error banner ─── -->
    <ElAlert
      v-if="error"
      data-testid="error-alert"
      title="Failed to load executions"
      :description="error"
      type="error"
      :closable="false"
      show-icon
    />

    <!-- ─── Filter bar ─── -->
    <ElCard class="eh-filter-card" shadow="never" data-testid="filter-bar">
      <ElRow :gutter="12">
        <ElCol :xs="24" :sm="12" :md="6">
          <div class="eh-filter-item">
            <label class="eh-filter-label">Serial Number</label>
            <ElInput
              v-model="serialNumber"
              placeholder="Search by DUT serial..."
              clearable
              size="small"
              data-testid="filter-serial"
              @keyup.enter="applyFilters"
            />
          </div>
        </ElCol>
        <ElCol :xs="24" :sm="12" :md="6">
          <div class="eh-filter-item">
            <label class="eh-filter-label">Product Type</label>
            <ElInput
              v-model="productType"
              placeholder="Filter by product type..."
              clearable
              size="small"
              data-testid="filter-product-type"
              @keyup.enter="applyFilters"
            />
          </div>
        </ElCol>
        <ElCol :xs="24" :sm="12" :md="5">
          <div class="eh-filter-item">
            <label class="eh-filter-label">Status</label>
            <ElSelect
              v-model="statusFilter"
              placeholder="All statuses"
              size="small"
              clearable
              data-testid="filter-status"
              class="eh-filter-select"
            >
              <ElOption
                v-for="opt in statusOptions"
                :key="opt.value"
                :label="opt.label"
                :value="opt.value"
              />
            </ElSelect>
          </div>
        </ElCol>
        <ElCol :xs="24" :sm="12" :md="5">
          <div class="eh-filter-item">
            <label class="eh-filter-label">Date Range</label>
            <ElDatePicker
              v-model="dateRange"
              type="datetimerange"
              size="small"
              start-placeholder="From"
              end-placeholder="To"
              format="YYYY-MM-DD HH:mm"
              value-format="YYYY-MM-DDTHH:mm:ss"
              class="eh-filter-datepicker"
              data-testid="filter-date-range"
            />
          </div>
        </ElCol>
        <ElCol :xs="24" :sm="24" :md="2">
          <div class="eh-filter-actions">
            <ElButton
              type="primary"
              size="small"
              :icon="Search"
              :loading="loading"
              @click="applyFilters"
              data-testid="btn-search"
            >
              Search
            </ElButton>
            <ElButton
              size="small"
              @click="resetFilters"
              data-testid="btn-reset"
            >
              Reset
            </ElButton>
          </div>
        </ElCol>
      </ElRow>
    </ElCard>

    <!-- ─── Loading skeleton ─── -->
    <div v-if="loading && executions.length === 0" data-testid="loading-skeleton">
      <ElSkeleton :rows="6" animated />
    </div>

    <!-- ─── Table ─── -->
    <ElCard v-else class="eh-table-card" shadow="never" data-testid="table-card">
      <ElEmpty
        v-if="executions.length === 0 && !loading"
        data-testid="empty-state"
        description="No executions found. Adjust filters and try again."
      />
      <ElTable
        v-else
        :data="executions"
        :row-key="rowKey"
        stripe
        size="small"
        class="eh-table"
        data-testid="exec-table"
        @row-click="openDetail"
      >
        <ElTableColumn label="Serial Number" min-width="160" data-testid="col-serial">
          <template #default="{ row }">
            <span class="eh-cell-serial">{{ row.dut_serial ?? '-' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="Product Type" min-width="120" data-testid="col-product-type">
          <template #default="{ row }">
            <span>{{ row.product_type ?? '-' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="Start Time" min-width="170" data-testid="col-start-time">
          <template #default="{ row }">
            <span>{{ formatDateTime(row.started_at) }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="Result" min-width="110" data-testid="col-result">
          <template #default="{ row }">
            <ElTag :type="statusTagType(row.status)" size="small">
              {{ row.status }}
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="Pass Rate" min-width="100" align="center" data-testid="col-pass-rate">
          <template #default="{ row }">
            <span :class="['eh-pass-rate', row.pass_rate !== null && row.pass_rate < 70 ? 'eh-pass-rate-low' : '']">
              {{ formatPassRate(row.pass_rate) }}
            </span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="Actions" width="100" align="center" data-testid="col-actions">
          <template #default="{ row }">
            <ElButton
              size="small"
              link
              type="primary"
              @click.stop="openDetail(row)"
              data-testid="btn-view-detail"
            >
              Details
            </ElButton>
          </template>
        </ElTableColumn>
      </ElTable>

      <!-- ─── Pagination ─── -->
      <div v-if="executions.length > 0" class="eh-pagination" data-testid="pagination-wrapper">
        <ElPagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next, jumper"
          size="small"
          background
          @current-change="onPaginationChange"
          @size-change="onPaginationChange"
          data-testid="pagination"
        />
      </div>
    </ElCard>

    <!-- ─── Detail Drawer ─── -->
    <ElDrawer
      v-model="drawerVisible"
      title="Execution Details"
      direction="rtl"
      size="55%"
      :before-close="(done: () => void) => { clearDetail(); done() }"
      data-testid="detail-drawer"
    >
      <!-- Detail loading -->
      <ElSkeleton v-if="detailLoading" :rows="8" animated data-testid="detail-loading" />

      <!-- Detail error -->
      <ElAlert
        v-if="detailError"
        data-testid="detail-error"
        title="Failed to load execution detail"
        :description="detailError"
        type="error"
        :closable="false"
        show-icon
      />

      <!-- Detail content -->
      <div v-if="detail" class="eh-detail" data-testid="detail-content">
        <!-- Summary header -->
        <div class="eh-detail-header" data-testid="detail-header">
          <div class="eh-detail-id">
            <span class="eh-detail-label">Run ID:</span>
            <span class="eh-detail-value">{{ detail.id }}</span>
          </div>
          <ElTag :type="statusTagType(detail.status)" size="small">{{ detail.status }}</ElTag>
          <span v-if="detail.dut_serial" class="eh-detail-serial">
            DUT: {{ detail.dut_serial }}
          </span>
        </div>

        <!-- Time info -->
        <div class="eh-detail-times" data-testid="detail-times">
          <div class="eh-detail-time-item">
            <span class="eh-detail-label">Started:</span>
            <span>{{ formatDateTime(detail.started_at) }}</span>
          </div>
          <div class="eh-detail-time-item">
            <span class="eh-detail-label">Completed:</span>
            <span>{{ formatDateTime(detail.completed_at) }}</span>
          </div>
        </div>

        <!-- Error message -->
        <div v-if="detail.error" class="eh-detail-error" data-testid="detail-error-msg">
          <ElAlert
            title="Execution Error"
            :description="detail.error"
            type="error"
            :closable="false"
            show-icon
          />
        </div>

        <!-- Step timeline -->
        <div class="eh-detail-section">
          <h3 class="eh-section-title">Step Timeline</h3>
          <div v-if="detail.step_results && detail.step_results.length > 0">
            <ElTimeline data-testid="step-timeline">
              <ElTimelineItem
                v-for="(step, idx) in detail.step_results"
                :key="step.step_id"
                :timestamp="formatDateTime(step.completed_at ?? step.started_at)"
                placement="top"
                :type="timelineTagType(step.status)"
                :hollow="step.status === 'PENDING'"
                :data-testid="`timeline-item-${idx}`"
              >
                <div class="eh-step">
                  <div class="eh-step-header">
                    <span class="eh-step-name">{{ step.name ?? step.step_id }}</span>
                    <ElTag size="small" :type="statusTagType(step.status)">{{ step.status }}</ElTag>
                  </div>
                  <div v-if="step.error" class="eh-step-error">{{ step.error }}</div>
                </div>
              </ElTimelineItem>
            </ElTimeline>
          </div>
          <ElEmpty v-else description="No step results recorded" :image-size="60" />
        </div>

        <!-- Measurement chart -->
        <div class="eh-detail-section">
          <h3 class="eh-section-title">Measurements</h3>
          <div class="eh-chart-container" data-testid="chart-container">
            <canvas ref="measurementCanvas" class="eh-canvas" data-testid="measurement-canvas"></canvas>
            <ElEmpty
              v-if="allMeasurements.length === 0"
              description="No measurements recorded"
              :image-size="40"
              class="eh-chart-empty"
            />
          </div>
        </div>

        <!-- Event log -->
        <div class="eh-detail-section">
          <h3 class="eh-section-title">Event Log</h3>
          <div v-if="eventLog.length > 0" class="eh-event-log" data-testid="event-log">
            <div
              v-for="(evt, idx) in eventLog"
              :key="evt.step_id"
              class="eh-event-row"
              :data-testid="`event-row-${idx}`"
            >
              <ElTag size="small" :type="statusTagType(evt.status)">{{ evt.status }}</ElTag>
              <span class="eh-event-name">{{ evt.name }}</span>
              <span class="eh-event-time">{{ formatDateTime(evt.time) }}</span>
              <span v-if="evt.error" class="eh-event-error">{{ evt.error }}</span>
            </div>
          </div>
          <ElEmpty v-else description="No events recorded" :image-size="60" />
        </div>
      </div>
    </ElDrawer>
  </div>
</template>

<style scoped>
.exec-history {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  padding: var(--spacing-md) var(--spacing-lg);
  min-height: 100vh;
  background-color: var(--color-bg-secondary);
}

/* ─── Header ─── */
.eh-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--spacing-sm);
}

.eh-title {
  font-size: 1.25rem;
  font-weight: 600;
  color: var(--color-text-primary);
  margin: 0;
}

.eh-header-right {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
}

/* ─── Filter bar ─── */
.eh-filter-card {
  background-color: var(--color-bg-primary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-xl);
}

.eh-filter-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.eh-filter-label {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--color-text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.03em;
}

.eh-filter-select,
.eh-filter-datepicker {
  width: 100%;
}

.eh-filter-actions {
  display: flex;
  align-items: flex-end;
  gap: var(--spacing-xs);
  height: 100%;
  padding-bottom: 1px;
}

/* ─── Table card ─── */
.eh-table-card {
  background-color: var(--color-bg-primary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-xl);
}

.eh-table {
  cursor: pointer;
}

.eh-cell-serial {
  font-family: monospace;
  font-size: 0.8125rem;
  color: var(--color-text-primary);
}

.eh-pass-rate {
  font-weight: 600;
  color: var(--color-text-primary);
}

.eh-pass-rate-low {
  color: #ef4444;
}

/* ─── Pagination ─── */
.eh-pagination {
  display: flex;
  justify-content: flex-end;
  padding-top: var(--spacing-sm);
}

/* ─── Detail drawer ─── */
.eh-detail {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.eh-detail-header {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  flex-wrap: wrap;
}

.eh-detail-id {
  display: flex;
  align-items: center;
  gap: 4px;
}

.eh-detail-label {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--color-text-secondary);
}

.eh-detail-value {
  font-family: monospace;
  font-size: 0.8125rem;
  color: var(--color-text-primary);
}

.eh-detail-serial {
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
  font-family: monospace;
}

.eh-detail-times {
  display: flex;
  gap: var(--spacing-md);
  flex-wrap: wrap;
}

.eh-detail-time-item {
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
  display: flex;
  gap: 4px;
}

.eh-detail-error {
  margin-top: var(--spacing-xs);
}

/* ─── Detail sections ─── */
.eh-detail-section {
  border-top: 1px solid var(--color-border-default);
  padding-top: var(--spacing-md);
}

.eh-section-title {
  font-size: 0.9375rem;
  font-weight: 600;
  color: var(--color-text-primary);
  margin: 0 0 var(--spacing-sm) 0;
}

/* ─── Step timeline ─── */
.eh-step {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.eh-step-header {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
}

.eh-step-name {
  font-weight: 500;
  color: var(--color-text-primary);
}

.eh-step-error {
  font-size: 0.75rem;
  color: #ef4444;
  padding: var(--spacing-xs) var(--spacing-sm);
  background-color: rgba(239, 68, 68, 0.05);
  border-radius: var(--radius-sm);
}

/* ─── Measurement chart ─── */
.eh-chart-container {
  position: relative;
  width: 100%;
  height: 240px;
}

.eh-canvas {
  width: 100%;
  height: 100%;
  display: block;
}

.eh-chart-empty {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
}

/* ─── Event log ─── */
.eh-event-log {
  display: flex;
  flex-direction: column;
  gap: 1px;
  background-color: var(--color-border-default);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-sm);
  overflow: hidden;
}

.eh-event-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  padding: var(--spacing-xs) var(--spacing-sm);
  background-color: var(--color-bg-primary);
  flex-wrap: wrap;
}

.eh-event-row:hover {
  background-color: var(--color-bg-secondary);
}

.eh-event-name {
  font-size: 0.8125rem;
  color: var(--color-text-primary);
  font-weight: 500;
  flex: 1;
}

.eh-event-time {
  font-size: 0.75rem;
  color: var(--color-text-secondary);
  font-family: monospace;
}

.eh-event-error {
  font-size: 0.75rem;
  color: #ef4444;
  flex-basis: 100%;
}

/* ─── Responsive ─── */
@media (max-width: 768px) {
  .exec-history {
    padding: var(--spacing-sm);
  }

  .eh-filter-actions {
    padding-top: var(--spacing-xs);
  }
}
</style>
