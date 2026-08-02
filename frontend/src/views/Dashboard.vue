<script setup lang="ts">
/**
 * Dashboard — production overview with 5-widget grid.
 *
 * Widgets:
 *   1. Active station count (el-statistic)
 *   2. Fault rate trend — SVG line chart (24h hourly buckets)
 *   3. Yield gauge — SVG gauge chart (pass rate %)
 *   4. Today's execution count (el-statistic)
 *   5. Top-5 fault Pareto — SVG bar chart
 *
 * Data flows from useDashboard composable which fetches all 4 API
 * endpoints in parallel and auto-refreshes every 30s.
 *
 * Route: /dashboard
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElCard, ElRow, ElCol, ElStatistic, ElSkeleton, ElAlert, ElEmpty, ElTag, ElButton, ElTable, ElTableColumn } from 'element-plus'
import { useDashboard } from '@/composables/useDashboard'
import SPCCharts from '@/components/SPCCharts.vue'
import { getSPCStatistics, getSPCChart, getSPCAlerts, type SPCStatistics, type SPCChart, type SPCAlert } from '@/api/spc'

// ─── Composable state ───────────────────────────────────────────────────────

const {
  summary,
  stations,
  faults,
  executions,
  loading,
  error,
  refresh,
} = useDashboard()

// ─── SPC state (for dashboard SPC section) ──────────────────────────────────

const spcStatistics = ref<SPCStatistics | null>(null)
const spcChart = ref<SPCChart | null>(null)
const spcAlerts = ref<SPCAlert[]>([])
const spcLoading = ref(false)
const spcError = ref<string | null>(null)

/** Default SPC product/measurement for dashboard preview. */
const SPC_DASHBOARD_PRODUCT = '5g_bsb'
const SPC_DASHBOARD_MEASUREMENT = 'voltage'

/** Fetch SPC data for dashboard preview. */
async function refreshSPC(): Promise<void> {
  spcLoading.value = true
  spcError.value = null
  try {
    const [stats, chart, alerts] = await Promise.all([
      getSPCStatistics(SPC_DASHBOARD_PRODUCT, SPC_DASHBOARD_MEASUREMENT),
      getSPCChart(SPC_DASHBOARD_PRODUCT, SPC_DASHBOARD_MEASUREMENT),
      getSPCAlerts(),
    ])
    spcStatistics.value = stats
    spcChart.value = chart
    spcAlerts.value = alerts
  } catch (e: unknown) {
    spcError.value = e instanceof Error ? e.message : String(e)
  } finally {
    spcLoading.value = false
  }
}

// ─── Chart refs ─────────────────────────────────────────────────────────────

const lineCanvas = ref<HTMLCanvasElement | null>(null)
const gaugeCanvas = ref<HTMLCanvasElement | null>(null)
const barCanvas = ref<HTMLCanvasElement | null>(null)

// ─── Computed values for widgets ────────────────────────────────────────────

const activeWorkerCount = computed(() => summary.value?.active_workers ?? 0)
const todayExecutionTotal = computed(() => summary.value?.total_executions_today ?? 0)
const passRate = computed(() => summary.value?.pass_rate ?? 0)
const totalFaults = computed(() => summary.value?.total_faults ?? 0)

const stationList = computed(() => stations.value?.stations ?? [])
const faultTrend = computed(() => faults.value?.trend ?? [])
const topFaults = computed(() => faults.value?.top_faults ?? [])
const recentExecutions = computed(() => executions.value?.recent ?? [])
const executionsByStatus = computed(() => executions.value?.by_status ?? {})

// ─── Line chart: fault rate trend ──────────────────────────────────────────

function drawLineChart(): void {
  const canvas = lineCanvas.value
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

  const data = faultTrend.value
  const padding = { top: 20, right: 20, bottom: 30, left: 40 }
  const chartW = w - padding.left - padding.right
  const chartH = h - padding.top - padding.bottom

  if (data.length === 0) return

  const maxCount = Math.max(...data.map((d) => d.count), 1)
  const stepX = chartW / Math.max(data.length - 1, 1)

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
    const val = Math.round(maxCount - (maxCount / 4) * i)
    ctx.fillStyle = 'rgba(0,0,0,0.4)'
    ctx.font = '10px sans-serif'
    ctx.textAlign = 'right'
    ctx.fillText(String(val), padding.left - 6, y + 3)
  }

  // Line path
  ctx.strokeStyle = getComputedStyle(document.documentElement)
    .getPropertyValue('--color-accent-purple')
    .trim() || '#aa3bff'
  ctx.lineWidth = 2
  ctx.lineJoin = 'round'
  ctx.beginPath()

  data.forEach((d, i) => {
    const x = padding.left + stepX * i
    const y = padding.top + chartH - (d.count / maxCount) * chartH
    if (i === 0) ctx.moveTo(x, y)
    else ctx.lineTo(x, y)
  })
  ctx.stroke()

  // Fill area under the line
  ctx.lineTo(padding.left + stepX * (data.length - 1), padding.top + chartH)
  ctx.lineTo(padding.left, padding.top + chartH)
  ctx.closePath()
  ctx.fillStyle = 'rgba(170, 59, 255, 0.1)'
  ctx.fill()

  // Data points
  ctx.fillStyle = getComputedStyle(document.documentElement)
    .getPropertyValue('--color-accent-purple')
    .trim() || '#aa3bff'
  data.forEach((d, i) => {
    const x = padding.left + stepX * i
    const y = padding.top + chartH - (d.count / maxCount) * chartH
    ctx.beginPath()
    ctx.arc(x, y, 3, 0, Math.PI * 2)
    ctx.fill()
  })
}

// ─── Gauge chart: yield (pass rate) ────────────────────────────────────────

function drawGaugeChart(): void {
  const canvas = gaugeCanvas.value
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

  const cx = w / 2
  const cy = h * 0.75
  const radius = Math.min(w, h * 1.5) * 0.38
  const startAngle = Math.PI
  const endAngle = 2 * Math.PI

  // Background arc
  ctx.strokeStyle = 'rgba(0,0,0,0.08)'
  ctx.lineWidth = 12
  ctx.lineCap = 'round'
  ctx.beginPath()
  ctx.arc(cx, cy, radius, startAngle, endAngle)
  ctx.stroke()

  // Value arc
  const value = Math.min(Math.max(passRate.value, 0), 100)
  const valueAngle = startAngle + (value / 100) * (endAngle - startAngle)

  // Color based on pass rate: green >= 90, orange >= 70, red < 70
  let arcColor = '#10b981'
  if (value < 70) arcColor = '#ef4444'
  else if (value < 90) arcColor = '#f59e0b'

  ctx.strokeStyle = arcColor
  ctx.lineWidth = 12
  ctx.beginPath()
  ctx.arc(cx, cy, radius, startAngle, valueAngle)
  ctx.stroke()

  // Center text
  ctx.fillStyle = getComputedStyle(document.documentElement)
    .getPropertyValue('--color-text-primary')
    .trim() || '#111827'
  ctx.font = 'bold 28px sans-serif'
  ctx.textAlign = 'center'
  ctx.textBaseline = 'middle'
  ctx.fillText(`${value.toFixed(1)}%`, cx, cy - 10)

  ctx.fillStyle = getComputedStyle(document.documentElement)
    .getPropertyValue('--color-text-secondary')
    .trim() || '#4b5563'
  ctx.font = '12px sans-serif'
  ctx.fillText('Pass Rate', cx, cy + 15)
}

// ─── Bar chart: Top-5 fault Pareto ─────────────────────────────────────────

function drawBarChart(): void {
  const canvas = barCanvas.value
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

  const data = topFaults.value
  const padding = { top: 20, right: 20, bottom: 60, left: 40 }
  const chartW = w - padding.left - padding.right
  const chartH = h - padding.top - padding.bottom

  if (data.length === 0) return

  const maxCount = Math.max(...data.map((d) => d.count), 1)
  const barWidth = chartW / data.length * 0.6
  const barGap = chartW / data.length * 0.4

  const colors = [
    '#aa3bff', '#8b5cf6', '#3b82f6', '#10b981', '#f59e0b',
  ]

  data.forEach((d, i) => {
    const x = padding.left + (chartW / data.length) * i + barGap / 2
    const barH = (d.count / maxCount) * chartH
    const y = padding.top + chartH - barH

    // Bar
    ctx.fillStyle = colors[i % colors.length] || '#aa3bff'
    ctx.fillRect(x, y, barWidth, barH)

    // Value label on top
    ctx.fillStyle = getComputedStyle(document.documentElement)
      .getPropertyValue('--color-text-primary')
      .trim() || '#111827'
    ctx.font = 'bold 11px sans-serif'
    ctx.textAlign = 'center'
    ctx.fillText(String(d.count), x + barWidth / 2, y - 5)

    // Category label (truncated)
    const label = d.category.length > 12 ? d.category.slice(0, 10) + '...' : d.category
    ctx.fillStyle = getComputedStyle(document.documentElement)
      .getPropertyValue('--color-text-secondary')
      .trim() || '#4b5563'
    ctx.font = '10px sans-serif'
    ctx.save()
    ctx.translate(x + barWidth / 2, padding.top + chartH + 8)
    ctx.rotate(-0.4)
    ctx.fillText(label, 0, 0)
    ctx.restore()
  })
}

// ─── Redraw charts when data changes ────────────────────────────────────────

watch([faultTrend, passRate, topFaults], () => {
  // Use nextTick to ensure DOM is updated
  requestAnimationFrame(() => {
    drawLineChart()
    drawGaugeChart()
    drawBarChart()
  })
})

// Redraw on window resize
function handleResize(): void {
  drawLineChart()
  drawGaugeChart()
  drawBarChart()
}

// ─── Status tag helper ──────────────────────────────────────────────────────

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

// ─── Lifecycle ──────────────────────────────────────────────────────────────

onMounted(() => {
  window.addEventListener('resize', handleResize)
  void refreshSPC()
})

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
})
</script>

<template>
  <div class="dashboard">
    <!-- ─── Header ─── -->
    <header class="dash-header">
      <div class="dash-header-left">
        <h1 class="dash-title">Production Dashboard</h1>
      </div>
      <div class="dash-header-right">
        <ElButton size="small" :loading="loading" @click="refresh" data-testid="btn-refresh">
          Refresh
        </ElButton>
      </div>
    </header>

    <!-- ─── Error banner ─── -->
    <ElAlert
      v-if="error"
      data-testid="error-alert"
      title="Failed to load dashboard data"
      :description="error"
      type="error"
      :closable="false"
      show-icon
    />

    <!-- ─── Loading skeleton ─── -->
    <div v-if="loading && !summary" data-testid="dashboard-skeleton">
      <ElSkeleton :rows="8" animated />
    </div>

    <!-- ─── Widget grid ─── -->
    <div v-else class="dash-grid">
      <ElRow :gutter="16">
        <!-- Widget 1: Active station count -->
        <ElCol :xs="24" :sm="12" :md="8" :lg="6">
          <ElCard class="dash-widget" shadow="never" data-testid="widget-active-stations">
            <template #header>
              <span class="dash-widget-title">Active Stations</span>
            </template>
            <div class="dash-stat-content">
              <ElStatistic :value="activeWorkerCount" data-testid="stat-active-stations" />
              <div class="dash-stat-detail">
                <ElTag type="success" size="small">Online</ElTag>
                <span class="dash-stat-sub">{{ stationList.length }} station(s)</span>
              </div>
            </div>
          </ElCard>
        </ElCol>

        <!-- Widget 4: Today's execution count -->
        <ElCol :xs="24" :sm="12" :md="8" :lg="6">
          <ElCard class="dash-widget" shadow="never" data-testid="widget-executions-today">
            <template #header>
              <span class="dash-widget-title">Today's Executions</span>
            </template>
            <div class="dash-stat-content">
              <ElStatistic :value="todayExecutionTotal" data-testid="stat-executions-today" />
              <div class="dash-stat-detail">
                <ElTag v-if="executionsByStatus.COMPLETED" type="success" size="small">
                  {{ executionsByStatus.COMPLETED }} done
                </ElTag>
                <ElTag v-if="executionsByStatus.FAILED" type="danger" size="small">
                  {{ executionsByStatus.FAILED }} failed
                </ElTag>
              </div>
            </div>
          </ElCard>
        </ElCol>

        <!-- Widget 2: Fault rate trend (line chart) -->
        <ElCol :xs="24" :sm="24" :md="24" :lg="12">
          <ElCard class="dash-widget" shadow="never" data-testid="widget-fault-trend">
            <template #header>
              <span class="dash-widget-title">Fault Rate Trend (24h)</span>
            </template>
            <div class="dash-chart-container">
              <canvas ref="lineCanvas" class="dash-canvas" data-testid="canvas-fault-trend"></canvas>
              <ElEmpty
                v-if="faultTrend.length === 0"
                description="No fault data"
                :image-size="40"
                class="dash-chart-empty"
              />
            </div>
          </ElCard>
        </ElCol>
      </ElRow>

      <ElRow :gutter="16" class="dash-row-second">
        <!-- Widget 3: Yield gauge -->
        <ElCol :xs="24" :sm="12" :md="8" :lg="8">
          <ElCard class="dash-widget" shadow="never" data-testid="widget-yield-gauge">
            <template #header>
              <span class="dash-widget-title">Yield (Pass Rate)</span>
            </template>
            <div class="dash-chart-container dash-chart-gauge">
              <canvas ref="gaugeCanvas" class="dash-canvas" data-testid="canvas-yield-gauge"></canvas>
            </div>
          </ElCard>
        </ElCol>

        <!-- Widget 5: Top-5 fault Pareto (bar chart) -->
        <ElCol :xs="24" :sm="12" :md="16" :lg="16">
          <ElCard class="dash-widget" shadow="never" data-testid="widget-top-faults">
            <template #header>
              <span class="dash-widget-title">Top-5 Fault Pareto</span>
            </template>
            <div class="dash-chart-container">
              <canvas ref="barCanvas" class="dash-canvas" data-testid="canvas-top-faults"></canvas>
              <ElEmpty
                v-if="topFaults.length === 0"
                description="No fault data"
                :image-size="40"
                class="dash-chart-empty"
              />
            </div>
          </ElCard>
        </ElCol>
      </ElRow>

      <!-- ─── Recent executions table ─── -->
      <ElRow :gutter="16" class="dash-row-third">
        <ElCol :span="24">
          <ElCard class="dash-widget" shadow="never" data-testid="widget-recent-executions">
            <template #header>
              <span class="dash-widget-title">Recent Executions (Today)</span>
            </template>
            <ElEmpty
              v-if="recentExecutions.length === 0"
              description="No executions today"
              data-testid="empty-recent-executions"
            />
            <div v-else class="dash-exec-list" data-testid="exec-list">
              <div
                v-for="ex in recentExecutions"
                :key="ex.id"
                class="dash-exec-item"
              >
                <span class="dash-exec-id">{{ ex.id.slice(0, 8) }}</span>
                <ElTag :type="statusTagType(ex.status)" size="small">{{ ex.status }}</ElTag>
                <span class="dash-exec-seq">{{ ex.sequence_id ?? '-' }}</span>
              </div>
            </div>
          </ElCard>
        </ElCol>
      </ElRow>

      <!-- ─── SPC Charts section ─── -->
      <ElRow :gutter="16" class="dash-row-third">
        <ElCol :span="24">
          <ElCard class="dash-widget" shadow="never" data-testid="widget-spc-charts">
            <template #header>
              <div class="dash-spc-header">
                <span class="dash-widget-title">SPC Control Charts — {{ SPC_DASHBOARD_PRODUCT }} / {{ SPC_DASHBOARD_MEASUREMENT }}</span>
                <ElTag v-if="spcAlerts.length > 0" type="danger" size="small" data-testid="spc-alert-count">
                  {{ spcAlerts.length }} alert(s)
                </ElTag>
              </div>
            </template>
            <SPCCharts
              :chart="spcChart"
              :statistics="spcStatistics"
              :loading="spcLoading"
            />
          </ElCard>
        </ElCol>
      </ElRow>
    </div>
  </div>
</template>

<style scoped>
.dashboard {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  padding: var(--spacing-md) var(--spacing-lg);
  min-height: 100vh;
  background-color: var(--color-bg-secondary);
}

/* ─── Header ─── */
.dash-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--spacing-sm);
}

.dash-title {
  font-size: 1.25rem;
  font-weight: 600;
  color: var(--color-text-primary);
  margin: 0;
}

.dash-header-right {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
}

/* ─── Grid ─── */
.dash-grid {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.dash-row-second,
.dash-row-third {
  margin-top: 0;
}

/* ─── Widget cards ─── */
.dash-widget {
  background-color: var(--color-bg-primary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-xl);
  margin-bottom: var(--spacing-md);
}

.dash-widget-title {
  font-size: 0.9375rem;
  font-weight: 600;
  color: var(--color-text-primary);
}

/* ─── Statistic content ─── */
.dash-stat-content {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
}

.dash-stat-detail {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
  flex-wrap: wrap;
}

.dash-stat-sub {
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
}

/* ─── Chart containers ─── */
.dash-chart-container {
  position: relative;
  width: 100%;
  height: 200px;
}

.dash-chart-gauge {
  height: 220px;
}

.dash-canvas {
  width: 100%;
  height: 100%;
  display: block;
}

.dash-chart-empty {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
}

/* ─── Recent executions ─── */
.dash-exec-list {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.dash-exec-item {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  padding: var(--spacing-xs) var(--spacing-sm);
  border-radius: var(--radius-md);
  background-color: var(--color-bg-tertiary);
}

.dash-exec-id {
  font-family: monospace;
  font-size: 0.8125rem;
  color: var(--color-text-primary);
  min-width: 80px;
}

.dash-exec-seq {
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
  flex: 1;
}

/* ─── SPC header ─── */
.dash-spc-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-sm);
}

/* ─── Responsive ─── */
@media (max-width: 768px) {
  .dashboard {
    padding: var(--spacing-sm);
  }

  .dash-chart-container {
    height: 160px;
  }
}
</style>
