<script setup lang="ts">
/**
 * SPCCharts — Statistical Process Control visualization component.
 *
 * Renders three Canvas-based charts:
 *   1. X-bar control chart (center line, UCL/LCL, data points, outliers highlighted)
 *   2. R chart (range chart with control limits)
 *   3. Cpk gauge (semicircle gauge with color zones: red <1.0, yellow 1.0-1.33, green >1.33)
 *
 * Western Electric rule violations are detected and highlighted:
 *   - Points beyond 3-sigma (beyond UCL/LCL)
 *   - 2 of 3 consecutive points beyond 2-sigma
 *   - 4 of 5 consecutive points beyond 1-sigma
 *   - 8 consecutive points on one side of center line
 *
 * Uses Canvas 2D API (no echarts dependency), following Dashboard.vue pattern.
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElCard, ElEmpty, ElSkeleton, ElTag } from 'element-plus'
import type { SPCChart, SPCStatistics } from '@/api/spc'

// ─── Props ───────────────────────────────────────────────────────────────────

interface Props {
  /** SPC chart data (subgroups, control limits). */
  chart: SPCChart | null
  /** SPC statistics (Cpk value). */
  statistics: SPCStatistics | null
  /** Loading state. */
  loading?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  loading: false,
})

// ─── Canvas refs ─────────────────────────────────────────────────────────────

const xbarCanvas = ref<HTMLCanvasElement | null>(null)
const rChartCanvas = ref<HTMLCanvasElement | null>(null)
const cpkCanvas = ref<HTMLCanvasElement | null>(null)

// ─── Computed ────────────────────────────────────────────────────────────────

const subgroups = computed(() => props.chart?.subgroups ?? [])
const hasData = computed(() => subgroups.value.length > 0)
const cpkValue = computed(() => props.statistics?.cpk ?? null)
const ppkValue = computed(() => props.statistics?.ppk ?? null)
const cpValue = computed(() => props.statistics?.cp ?? null)
const meanValue = computed(() => props.statistics?.mean ?? null)
const sampleCount = computed(() => props.statistics?.sample_count ?? 0)

/** Detect Western Electric rule violations for outlier highlighting. */
const outlierIndices = computed<Set<number>>(() => {
  const result = new Set<number>()
  const sg = subgroups.value
  if (sg.length === 0) return result

  const cl = props.chart?.center_line
  const ucl = props.chart?.ucl
  const lcl = props.chart?.lcl
  if (cl == null || ucl == null || lcl == null) return result

  // 3-sigma band boundaries
  const sigma3High = ucl
  const sigma3Low = lcl
  // 2-sigma band
  const range = (ucl - lcl) / 2
  const sigma2High = cl + (range * 2 / 3)
  const sigma2Low = cl - (range * 2 / 3)
  // 1-sigma band
  const sigma1High = cl + (range / 3)
  const sigma1Low = cl - (range / 3)

  for (let i = 0; i < sg.length; i++) {
    const v = sg[i].mean
    // Rule 1: Beyond 3-sigma
    if (v > sigma3High || v < sigma3Low) {
      result.add(i)
      continue
    }
    // Rule 2: 2 of 3 beyond 2-sigma (same side)
    if (i >= 2) {
      const w = [sg[i - 2].mean, sg[i - 1].mean, v]
      const above = w.filter((x) => x > sigma2High).length
      const below = w.filter((x) => x < sigma2Low).length
      if (above >= 2 || below >= 2) {
        result.add(i)
        continue
      }
    }
    // Rule 3: 4 of 5 beyond 1-sigma (same side)
    if (i >= 4) {
      const w = [sg[i - 4].mean, sg[i - 3].mean, sg[i - 2].mean, sg[i - 1].mean, v]
      const above = w.filter((x) => x > sigma1High).length
      const below = w.filter((x) => x < sigma1Low).length
      if (above >= 4 || below >= 4) {
        result.add(i)
        continue
      }
    }
    // Rule 4: 8 consecutive on one side of center
    if (i >= 7) {
      const w = [sg[i - 7].mean, sg[i - 6].mean, sg[i - 5].mean, sg[i - 4].mean,
                 sg[i - 3].mean, sg[i - 2].mean, sg[i - 1].mean, v]
      const above = w.filter((x) => x > cl).length
      const below = w.filter((x) => x < cl).length
      if (above >= 8 || below >= 8) {
        result.add(i)
      }
    }
  }
  return result
})

/** Cpk gauge zone color. */
const cpkColor = computed<string>(() => {
  const v = cpkValue.value
  if (v == null) return '#9ca3af'
  if (v < 1.0) return '#ef4444'
  if (v < 1.33) return '#f59e0b'
  return '#10b981'
})

const cpkLabel = computed<string>(() => {
  const v = cpkValue.value
  if (v == null) return 'N/A'
  if (v < 1.0) return 'Incapable'
  if (v < 1.33) return 'Marginal'
  return 'Capable'
})

// ─── Canvas drawing: X-bar chart ─────────────────────────────────────────────

function drawXbarChart(): void {
  const canvas = xbarCanvas.value
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

  const sg = subgroups.value
  if (sg.length === 0) return

  const padding = { top: 20, right: 20, bottom: 30, left: 50 }
  const chartW = w - padding.left - padding.right
  const chartH = h - padding.top - padding.bottom

  const cl = props.chart?.center_line
  const ucl = props.chart?.ucl
  const lcl = props.chart?.lcl
  if (cl == null || ucl == null || lcl == null) return

  const allVals = [...sg.map((s) => s.mean), ucl, lcl, cl]
  const maxVal = Math.max(...allVals)
  const minVal = Math.min(...allVals)
  const dataRange = maxVal - minVal || 1
  const padRange = dataRange * 0.15
  const yMax = maxVal + padRange
  const yMin = minVal - padRange
  const yRange = yMax - yMin || 1

  const stepX = chartW / Math.max(sg.length - 1, 1)

  function yFor(val: number): number {
    return padding.top + chartH - ((val - yMin) / yRange) * chartH
  }

  // Grid lines
  ctx.strokeStyle = 'rgba(0,0,0,0.06)'
  ctx.lineWidth = 1
  for (let i = 0; i <= 4; i++) {
    const y = padding.top + (chartH / 4) * i
    ctx.beginPath()
    ctx.moveTo(padding.left, y)
    ctx.lineTo(padding.left + chartW, y)
    ctx.stroke()

    const val = yMax - (yRange / 4) * i
    ctx.fillStyle = 'rgba(0,0,0,0.4)'
    ctx.font = '10px sans-serif'
    ctx.textAlign = 'right'
    ctx.fillText(val.toFixed(2), padding.left - 6, y + 3)
  }

  // UCL (red dashed)
  ctx.strokeStyle = '#ef4444'
  ctx.lineWidth = 1.5
  ctx.setLineDash([6, 4])
  ctx.beginPath()
  ctx.moveTo(padding.left, yFor(ucl))
  ctx.lineTo(padding.left + chartW, yFor(ucl))
  ctx.stroke()

  // LCL (red dashed)
  ctx.beginPath()
  ctx.moveTo(padding.left, yFor(lcl))
  ctx.lineTo(padding.left + chartW, yFor(lcl))
  ctx.stroke()

  // Center line (green solid)
  ctx.strokeStyle = '#10b981'
  ctx.lineWidth = 1.5
  ctx.setLineDash([])
  ctx.beginPath()
  ctx.moveTo(padding.left, yFor(cl))
  ctx.lineTo(padding.left + chartW, yFor(cl))
  ctx.stroke()

  // Limit labels
  ctx.fillStyle = '#ef4444'
  ctx.font = '9px sans-serif'
  ctx.textAlign = 'left'
  ctx.fillText(`UCL ${ucl.toFixed(3)}`, padding.left + chartW - 60, yFor(ucl) - 4)
  ctx.fillText(`LCL ${lcl.toFixed(3)}`, padding.left + chartW - 60, yFor(lcl) + 12)
  ctx.fillStyle = '#10b981'
  ctx.fillText(`CL ${cl.toFixed(3)}`, padding.left + chartW - 60, yFor(cl) - 4)

  // Data line
  ctx.strokeStyle = getComputedStyle(document.documentElement)
    .getPropertyValue('--color-accent-purple')
    .trim() || '#aa3bff'
  ctx.lineWidth = 1.5
  ctx.lineJoin = 'round'
  ctx.beginPath()
  sg.forEach((s, i) => {
    const x = padding.left + stepX * i
    const y = yFor(s.mean)
    if (i === 0) ctx.moveTo(x, y)
    else ctx.lineTo(x, y)
  })
  ctx.stroke()

  // Data points — outliers get red filled circles with ring
  sg.forEach((s, i) => {
    const x = padding.left + stepX * i
    const y = yFor(s.mean)
    const isOutlier = outlierIndices.value.has(i)

    if (isOutlier) {
      // Red ring around outlier
      ctx.strokeStyle = '#ef4444'
      ctx.lineWidth = 2
      ctx.beginPath()
      ctx.arc(x, y, 7, 0, Math.PI * 2)
      ctx.stroke()
      // Red filled dot
      ctx.fillStyle = '#ef4444'
      ctx.beginPath()
      ctx.arc(x, y, 4, 0, Math.PI * 2)
      ctx.fill()
    } else {
      // Normal point
      ctx.fillStyle = getComputedStyle(document.documentElement)
        .getPropertyValue('--color-accent-purple')
        .trim() || '#aa3bff'
      ctx.beginPath()
      ctx.arc(x, y, 3, 0, Math.PI * 2)
      ctx.fill()
    }
  })
}

// ─── Canvas drawing: R chart ─────────────────────────────────────────────────

function drawRChart(): void {
  const canvas = rChartCanvas.value
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

  const sg = subgroups.value
  if (sg.length === 0) return

  const padding = { top: 20, right: 20, bottom: 30, left: 50 }
  const chartW = w - padding.left - padding.right
  const chartH = h - padding.top - padding.bottom

  const rCenter = props.chart?.r_center
  const rUcl = props.chart?.r_ucl
  const rLcl = props.chart?.r_lcl
  if (rCenter == null) return

  const ranges = sg.map((s) => s.range)
  const maxVal = Math.max(...ranges, rUcl ?? 0, rCenter)
  const minVal = Math.min(...ranges, rLcl ?? 0, 0)
  const dataRange = maxVal - minVal || 1
  const padRange = dataRange * 0.15
  const yMax = maxVal + padRange
  const yMin = Math.max(0, minVal - padRange)
  const yRange = yMax - yMin || 1

  const stepX = chartW / Math.max(sg.length - 1, 1)

  function yFor(val: number): number {
    return padding.top + chartH - ((val - yMin) / yRange) * chartH
  }

  // Grid lines
  ctx.strokeStyle = 'rgba(0,0,0,0.06)'
  ctx.lineWidth = 1
  for (let i = 0; i <= 4; i++) {
    const y = padding.top + (chartH / 4) * i
    ctx.beginPath()
    ctx.moveTo(padding.left, y)
    ctx.lineTo(padding.left + chartW, y)
    ctx.stroke()

    const val = yMax - (yRange / 4) * i
    ctx.fillStyle = 'rgba(0,0,0,0.4)'
    ctx.font = '10px sans-serif'
    ctx.textAlign = 'right'
    ctx.fillText(val.toFixed(2), padding.left - 6, y + 3)
  }

  // R-UCL (red dashed)
  if (rUcl != null) {
    ctx.strokeStyle = '#ef4444'
    ctx.lineWidth = 1.5
    ctx.setLineDash([6, 4])
    ctx.beginPath()
    ctx.moveTo(padding.left, yFor(rUcl))
    ctx.lineTo(padding.left + chartW, yFor(rUcl))
    ctx.stroke()
  }

  // R-LCL (red dashed)
  if (rLcl != null) {
    ctx.beginPath()
    ctx.moveTo(padding.left, yFor(rLcl))
    ctx.lineTo(padding.left + chartW, yFor(rLcl))
    ctx.stroke()
  }

  // R center line (green solid)
  ctx.strokeStyle = '#10b981'
  ctx.lineWidth = 1.5
  ctx.setLineDash([])
  ctx.beginPath()
  ctx.moveTo(padding.left, yFor(rCenter))
  ctx.lineTo(padding.left + chartW, yFor(rCenter))
  ctx.stroke()

  // Limit labels
  ctx.fillStyle = '#ef4444'
  ctx.font = '9px sans-serif'
  ctx.textAlign = 'left'
  if (rUcl != null) ctx.fillText(`UCL ${rUcl.toFixed(3)}`, padding.left + chartW - 60, yFor(rUcl) - 4)
  if (rLcl != null) ctx.fillText(`LCL ${rLcl.toFixed(3)}`, padding.left + chartW - 60, yFor(rLcl) + 12)
  ctx.fillStyle = '#10b981'
  ctx.fillText(`R̄ ${rCenter.toFixed(3)}`, padding.left + chartW - 60, yFor(rCenter) - 4)

  // Data line
  ctx.strokeStyle = '#3b82f6'
  ctx.lineWidth = 1.5
  ctx.lineJoin = 'round'
  ctx.beginPath()
  sg.forEach((s, i) => {
    const x = padding.left + stepX * i
    const y = yFor(s.range)
    if (i === 0) ctx.moveTo(x, y)
    else ctx.lineTo(x, y)
  })
  ctx.stroke()

  // Data points — highlight range outliers
  sg.forEach((s, i) => {
    const x = padding.left + stepX * i
    const y = yFor(s.range)
    const isOutlier =
      (rUcl != null && s.range > rUcl) ||
      (rLcl != null && s.range < rLcl)

    if (isOutlier) {
      ctx.strokeStyle = '#ef4444'
      ctx.lineWidth = 2
      ctx.beginPath()
      ctx.arc(x, y, 7, 0, Math.PI * 2)
      ctx.stroke()
      ctx.fillStyle = '#ef4444'
      ctx.beginPath()
      ctx.arc(x, y, 4, 0, Math.PI * 2)
      ctx.fill()
    } else {
      ctx.fillStyle = '#3b82f6'
      ctx.beginPath()
      ctx.arc(x, y, 3, 0, Math.PI * 2)
      ctx.fill()
    }
  })
}

// ─── Canvas drawing: Cpk gauge ───────────────────────────────────────────────

function drawCpkGauge(): void {
  const canvas = cpkCanvas.value
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

  // Zone arcs (background)
  // Red zone: 0 to 1.0 (0% to 33.3% of semicircle)
  // Yellow zone: 1.0 to 1.33 (33.3% to 44.3%)
  // Green zone: 1.33 to 2.0 (44.3% to 66.7%)
  // Remaining (2.0+) to 3.0: green continuation
  const maxScale = 3.0
  const redEnd = startAngle + (1.0 / maxScale) * (endAngle - startAngle)
  const yellowEnd = startAngle + (1.33 / maxScale) * (endAngle - startAngle)

  // Red zone
  ctx.strokeStyle = '#ef4444'
  ctx.lineWidth = 14
  ctx.lineCap = 'butt'
  ctx.beginPath()
  ctx.arc(cx, cy, radius, startAngle, redEnd)
  ctx.stroke()

  // Yellow zone
  ctx.strokeStyle = '#f59e0b'
  ctx.beginPath()
  ctx.arc(cx, cy, radius, redEnd, yellowEnd)
  ctx.stroke()

  // Green zone
  ctx.strokeStyle = '#10b981'
  ctx.beginPath()
  ctx.arc(cx, cy, radius, yellowEnd, endAngle)
  ctx.stroke()

  // Value pointer
  const v = cpkValue.value
  if (v != null) {
    const clamped = Math.min(Math.max(v, 0), maxScale)
    const valueAngle = startAngle + (clamped / maxScale) * (endAngle - startAngle)

    // Value arc overlay
    ctx.strokeStyle = cpkColor.value
    ctx.lineWidth = 8
    ctx.beginPath()
    ctx.arc(cx, cy, radius, startAngle, valueAngle)
    ctx.stroke()

    // Pointer line
    ctx.strokeStyle = getComputedStyle(document.documentElement)
      .getPropertyValue('--color-text-primary')
      .trim() || '#111827'
    ctx.lineWidth = 2
    ctx.beginPath()
    ctx.moveTo(cx, cy)
    const px = cx + Math.cos(valueAngle) * (radius - 4)
    const py = cy + Math.sin(valueAngle) * (radius - 4)
    ctx.lineTo(px, py)
    ctx.stroke()

    // Center dot
    ctx.fillStyle = getComputedStyle(document.documentElement)
      .getPropertyValue('--color-text-primary')
      .trim() || '#111827'
    ctx.beginPath()
    ctx.arc(cx, cy, 4, 0, Math.PI * 2)
    ctx.fill()
  }

  // Center text — Cpk value
  ctx.fillStyle = cpkColor.value
  ctx.font = 'bold 24px sans-serif'
  ctx.textAlign = 'center'
  ctx.textBaseline = 'middle'
  ctx.fillText(v != null ? v.toFixed(3) : 'N/A', cx, cy - 10)

  ctx.fillStyle = getComputedStyle(document.documentElement)
    .getPropertyValue('--color-text-secondary')
    .trim() || '#4b5563'
  ctx.font = '11px sans-serif'
  ctx.fillText('Cpk', cx, cy + 12)

  // Zone labels
  ctx.fillStyle = 'rgba(0,0,0,0.35)'
  ctx.font = '9px sans-serif'
  ctx.textAlign = 'center'
  ctx.fillText('1.0', cx + Math.cos(redEnd) * (radius + 12), cy + Math.sin(redEnd) * (radius + 12))
  ctx.fillText('1.33', cx + Math.cos(yellowEnd) * (radius + 12), cy + Math.sin(yellowEnd) * (radius + 12))
}

// ─── Redraw on data change ───────────────────────────────────────────────────

function redrawAll(): void {
  requestAnimationFrame(() => {
    drawXbarChart()
    drawRChart()
    drawCpkGauge()
  })
}

watch([subgroups, cpkValue, () => props.chart, () => props.statistics], () => {
  redrawAll()
})

function handleResize(): void {
  redrawAll()
}

// ─── Lifecycle ───────────────────────────────────────────────────────────────

onMounted(() => {
  window.addEventListener('resize', handleResize)
  redrawAll()
})

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
})
</script>

<template>
  <div class="spc-charts" data-testid="spc-charts">
    <!-- ─── Loading ─── -->
    <div v-if="loading && !hasData" data-testid="spc-loading">
      <ElSkeleton :rows="6" animated />
    </div>

    <!-- ─── Empty state ─── -->
    <div v-else-if="!hasData" data-testid="spc-empty" class="spc-empty-state">
      <ElEmpty description="No SPC data available for this measurement stream" :image-size="60" />
    </div>

    <!-- ─── Charts ─── -->
    <div v-else class="spc-charts-grid" data-testid="spc-charts-grid">
      <!-- X-bar chart -->
      <ElCard class="spc-chart-card" shadow="never" data-testid="card-xbar-chart">
        <template #header>
          <div class="spc-chart-header">
            <span class="spc-chart-title">X-bar Control Chart</span>
            <ElTag v-if="outlierIndices.size > 0" type="danger" size="small" data-testid="xbar-outlier-count">
              {{ outlierIndices.size }} outlier(s)
            </ElTag>
          </div>
        </template>
        <div class="spc-chart-container">
          <canvas ref="xbarCanvas" class="spc-canvas" data-testid="canvas-xbar-chart"></canvas>
        </div>
      </ElCard>

      <!-- R chart -->
      <ElCard class="spc-chart-card" shadow="never" data-testid="card-r-chart">
        <template #header>
          <span class="spc-chart-title">Range (R) Chart</span>
        </template>
        <div class="spc-chart-container">
          <canvas ref="rChartCanvas" class="spc-canvas" data-testid="canvas-r-chart"></canvas>
        </div>
      </ElCard>

      <!-- Cpk gauge -->
      <ElCard class="spc-chart-card spc-gauge-card" shadow="never" data-testid="card-cpk-gauge">
        <template #header>
          <span class="spc-chart-title">Cpk Capability Gauge</span>
        </template>
        <div class="spc-chart-container spc-gauge-container">
          <canvas ref="cpkCanvas" class="spc-canvas" data-testid="canvas-cpk-gauge"></canvas>
        </div>
        <div class="spc-stats-row" data-testid="spc-stats-row">
          <div class="spc-stat-item">
            <span class="spc-stat-label">Cpk</span>
            <span class="spc-stat-value" :style="{ color: cpkColor }">{{ cpkValue != null ? cpkValue.toFixed(3) : 'N/A' }}</span>
            <ElTag :type="cpkValue != null && cpkValue < 1.0 ? 'danger' : cpkValue != null && cpkValue < 1.33 ? 'warning' : 'success'" size="small">
              {{ cpkLabel }}
            </ElTag>
          </div>
          <div class="spc-stat-item">
            <span class="spc-stat-label">Cp</span>
            <span class="spc-stat-value">{{ cpValue != null ? cpValue.toFixed(3) : 'N/A' }}</span>
          </div>
          <div class="spc-stat-item">
            <span class="spc-stat-label">Ppk</span>
            <span class="spc-stat-value">{{ ppkValue != null ? ppkValue.toFixed(3) : 'N/A' }}</span>
          </div>
          <div class="spc-stat-item">
            <span class="spc-stat-label">Mean</span>
            <span class="spc-stat-value">{{ meanValue != null ? meanValue.toFixed(4) : 'N/A' }}</span>
          </div>
          <div class="spc-stat-item">
            <span class="spc-stat-label">Samples</span>
            <span class="spc-stat-value">{{ sampleCount }}</span>
          </div>
        </div>
      </ElCard>
    </div>
  </div>
</template>

<style scoped>
.spc-charts {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.spc-charts-grid {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.spc-chart-card {
  background-color: var(--color-bg-primary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-xl);
  margin-bottom: 0;
}

.spc-chart-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-sm);
}

.spc-chart-title {
  font-size: 0.9375rem;
  font-weight: 600;
  color: var(--color-text-primary);
}

.spc-chart-container {
  position: relative;
  width: 100%;
  height: 200px;
}

.spc-gauge-container {
  height: 180px;
}

.spc-canvas {
  width: 100%;
  height: 100%;
  display: block;
}

.spc-empty-state {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--spacing-lg) 0;
}

/* ─── Stats row ─── */
.spc-stats-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-md);
  padding-top: var(--spacing-sm);
  border-top: 1px solid var(--color-border-default);
}

.spc-stat-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  min-width: 60px;
}

.spc-stat-label {
  font-size: 0.6875rem;
  color: var(--color-text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.spc-stat-value {
  font-size: 0.9375rem;
  font-weight: 600;
  color: var(--color-text-primary);
  font-family: monospace;
}

/* ─── Responsive ─── */
@media (max-width: 768px) {
  .spc-chart-container {
    height: 160px;
  }

  .spc-gauge-container {
    height: 150px;
  }

  .spc-stats-row {
    gap: var(--spacing-sm);
  }
}
</style>
