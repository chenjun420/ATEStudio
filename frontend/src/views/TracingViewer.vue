<script setup lang="ts">
/**
 * Test Traceability Viewer.
 *
 * Searches for a DUT serial number and renders the full trace chain as a
 * Gantt-like vertical timeline:
 *
 *   DUT serial number (root)
 *     -> execution step 1 (station, status, time range)
 *        -> instruments used
 *        -> measurements (name, value, limits, outcome)
 *     -> execution step 2 ...
 *
 * Two endpoints are consumed:
 *   - GET /api/v1/trace/{serial_number}/structured  - the structured
 *     TestTraceResult used to render the timeline.
 *   - GET /api/v1/trace/{serial_number}             - the W3C PROV JSON-LD
 *     document, shown in a collapsible raw-viewer for PROV-aware consumers.
 *
 * Route: /trace
 */
import { computed, onMounted, ref } from 'vue'
import {
  ElInput,
  ElButton,
  ElCard,
  ElTag,
  ElEmpty,
  ElSkeleton,
  ElAlert,
  ElCollapse,
  ElCollapseItem,
  ElTimeline,
  ElTimelineItem,
  ElIcon,
} from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import http from '@/api/interceptor'

// ─── Types (mirror src/ate_cloud/schemas/trace.py) ───────────────────────────

interface TraceInstrument {
  instrument_id: string
}

interface TraceMeasurement {
  measurement_id: string
  name: string
  value: number | null
  unit: string | null
  limits_min: number | null
  limits_max: number | null
  outcome: string
  timestamp: string
}

interface TraceStep {
  execution_id: string
  sequence_id: string | null
  station_id: string | null
  status: string
  started_at: string | null
  completed_at: string | null
  instruments: TraceInstrument[]
  measurements: TraceMeasurement[]
}

interface TestTraceResult {
  dut_serial: string
  steps: TraceStep[]
}

// ─── State ───────────────────────────────────────────────────────────────────

const searchInput = ref<string>('')
const committedSerial = ref<string>('')
const trace = ref<TestTraceResult | null>(null)
const jsonld = ref<Record<string, unknown> | null>(null)
const loading = ref<boolean>(false)
const error = ref<string | null>(null)
const showJsonld = ref<boolean>(false)

// ─── Computed ────────────────────────────────────────────────────────────────

/** Whether a search has been committed (distinguishes initial empty state). */
const hasSearched = computed<boolean>(() => committedSerial.value !== '')

/** Total measurement count across all steps (summary badge). */
const totalMeasurements = computed<number>(() =>
  (trace.value?.steps ?? []).reduce((sum, s) => sum + s.measurements.length, 0),
)

/** Total instrument count across all steps (deduplicated). */
const totalInstruments = computed<number>(() => {
  const steps = trace.value?.steps ?? []
  const ids = new Set<string>()
  for (const step of steps) {
    for (const inst of step.instruments) ids.add(inst.instrument_id)
  }
  return ids.size
})

/** PASS/FAIL summary across all measurements. */
const outcomeSummary = computed<{ PASS: number; FAIL: number; WARNING: number; OTHER: number }>(() => {
  const counts = { PASS: 0, FAIL: 0, WARNING: 0, OTHER: 0 }
  for (const step of trace.value?.steps ?? []) {
    for (const m of step.measurements) {
      if (m.outcome === 'PASS') counts.PASS++
      else if (m.outcome === 'FAIL') counts.FAIL++
      else if (m.outcome === 'WARNING') counts.WARNING++
      else counts.OTHER++
    }
  }
  return counts
})

// ─── Helpers ─────────────────────────────────────────────────────────────────

function statusTagType(
  status: string,
): 'success' | 'warning' | 'danger' | 'info' {
  switch (status) {
    case 'COMPLETED':
      return 'success'
    case 'RUNNING':
      return 'warning'
    case 'FAILED':
      return 'danger'
    case 'ABORTED':
      return 'danger'
    case 'PENDING':
      return 'info'
    case 'UNKNOWN':
      return 'info'
    default:
      return 'info'
  }
}

function outcomeTagType(
  outcome: string,
): 'success' | 'warning' | 'danger' | 'info' {
  switch (outcome) {
    case 'PASS':
      return 'success'
    case 'WARNING':
      return 'warning'
    case 'FAIL':
      return 'danger'
    default:
      return 'info'
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

/** Format a measurement value with its unit and limits range. */
function formatMeasurement(m: TraceMeasurement): string {
  const val = m.value !== null ? m.value.toFixed(4).replace(/\.?0+$/, '') : 'n/a'
  const unit = m.unit ?? ''
  const lim =
    m.limits_min !== null || m.limits_max !== null
      ? ` [${m.limits_min ?? '-∞'}, ${m.limits_max ?? '+∞'}]`
      : ''
  return `${val} ${unit}${lim}`.trim()
}

/** Duration between started_at and completed_at, human-readable. */
function duration(step: TraceStep): string {
  if (!step.started_at || !step.completed_at) return '-'
  const start = new Date(step.started_at).getTime()
  const end = new Date(step.completed_at).getTime()
  const ms = end - start
  if (ms < 0) return '-'
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  if (ms < 3_600_000) return `${(ms / 60_000).toFixed(1)}m`
  return `${(ms / 3_600_000).toFixed(1)}h`
}

// ─── Data fetching ───────────────────────────────────────────────────────────

async function search(): Promise<void> {
  const serial = searchInput.value.trim()
  if (!serial) return
  committedSerial.value = serial
  loading.value = true
  error.value = null
  trace.value = null
  jsonld.value = null
  try {
    const [structResp, jsonldResp] = await Promise.all([
      http.get<TestTraceResult>(`/trace/${encodeURIComponent(serial)}/structured`),
      http.get<Record<string, unknown>>(`/trace/${encodeURIComponent(serial)}`),
    ])
    trace.value = structResp.data
    jsonld.value = jsonldResp.data
  } catch (e: unknown) {
    if (e && typeof e === 'object' && 'response' in e) {
      const resp = (e as { response?: { status?: number; data?: { detail?: string } } }).response
      if (resp?.status === 404) {
        error.value = `No trace data found for serial number '${serial}'`
      } else {
        error.value = resp?.data?.detail ?? `Request failed (${resp?.status ?? 'unknown'})`
      }
    } else {
      error.value = e instanceof Error ? e.message : String(e)
    }
  } finally {
    loading.value = false
  }
}

// ─── Mount ───────────────────────────────────────────────────────────────────

onMounted(() => {
  // No auto-search; the user enters a serial number.
})
</script>

<template>
  <div class="tracing-viewer">
    <!-- ─── Header / Search ─── -->
    <header class="tv-header">
      <h1 class="tv-title">Test Traceability</h1>
      <div class="tv-search">
        <ElInput
          v-model="searchInput"
          placeholder="Enter DUT serial number..."
          class="tv-search-input"
          clearable
          data-testid="search-input"
          @keyup.enter="search"
        >
          <template #append>
            <ElButton :icon="Search" :loading="loading" @click="search" data-testid="search-btn">
              Search
            </ElButton>
          </template>
        </ElInput>
      </div>
    </header>

    <!-- ─── Summary badges (after search) ─── -->
    <div v-if="trace" class="tv-summary">
      <ElTag type="info" size="large" data-testid="summary-serial">
        DUT: {{ trace.dut_serial }}
      </ElTag>
      <ElTag type="info" size="small" data-testid="summary-steps">
        Steps: {{ trace.steps.length }}
      </ElTag>
      <ElTag type="info" size="small" data-testid="summary-instruments">
        Instruments: {{ totalInstruments }}
      </ElTag>
      <ElTag type="info" size="small" data-testid="summary-measurements">
        Measurements: {{ totalMeasurements }}
      </ElTag>
      <ElTag type="success" size="small" data-testid="summary-pass">
        PASS: {{ outcomeSummary.PASS }}
      </ElTag>
      <ElTag type="danger" size="small" data-testid="summary-fail">
        FAIL: {{ outcomeSummary.FAIL }}
      </ElTag>
      <ElTag v-if="outcomeSummary.WARNING > 0" type="warning" size="small">
        WARN: {{ outcomeSummary.WARNING }}
      </ElTag>
    </div>

    <!-- ─── Error banner ─── -->
    <ElAlert
      v-if="error"
      data-testid="error-alert"
      title="Trace lookup failed"
      :description="error"
      type="error"
      :closable="false"
      show-icon
    />

    <!-- ─── Loading skeleton ─── -->
    <ElSkeleton v-if="loading" :rows="6" animated data-testid="loading-skeleton" />

    <!-- ─── Empty state (before first search) ─── -->
    <ElEmpty
      v-else-if="!trace && !error && !hasSearched"
      data-testid="empty-initial"
      description="Enter a DUT serial number to rebuild its trace chain."
    />

    <!-- ─── Timeline (Gantt-like) ─── -->
    <ElCard
      v-else-if="trace"
      class="tv-timeline-card"
      shadow="never"
      data-testid="timeline-card"
    >
      <template #header>
        <div class="tv-card-header">
          <span class="tv-card-title">Trace Timeline</span>
          <ElButton
            size="small"
            link
            @click="showJsonld = !showJsonld"
            data-testid="toggle-jsonld"
          >
            {{ showJsonld ? 'Hide' : 'Show' }} JSON-LD (W3C PROV)
          </ElButton>
        </div>
      </template>

      <ElTimeline v-if="trace.steps.length > 0" data-testid="trace-timeline">
        <ElTimelineItem
          v-for="(step, idx) in trace.steps"
          :key="step.execution_id"
          :timestamp="formatDateTime(step.started_at)"
          placement="top"
          :type="statusTagType(step.status) === 'danger' ? 'danger' : statusTagType(step.status) === 'success' ? 'success' : 'primary'"
          :hollow="step.status === 'PENDING'"
          data-testid="timeline-step"
        >
          <div class="tv-step" :data-testid="`step-${idx}`">
            <!-- Step header: execution id, station, status, duration -->
            <div class="tv-step-header">
              <span class="tv-step-exec">Execution {{ step.execution_id }}</span>
              <ElTag size="small" :type="statusTagType(step.status)">
                {{ step.status }}
              </ElTag>
              <span v-if="step.station_id" class="tv-step-station">
                <ElIcon><Search /></ElIcon>
                {{ step.station_id }}
              </span>
              <span v-if="step.sequence_id" class="tv-step-seq">
                seq: {{ step.sequence_id }}
              </span>
              <span class="tv-step-duration">
                {{ formatDateTime(step.started_at) }} → {{ formatDateTime(step.completed_at) }}
                <ElTag size="small" type="info">{{ duration(step) }}</ElTag>
              </span>
            </div>

            <!-- Instruments row -->
            <div v-if="step.instruments.length > 0" class="tv-step-instruments">
              <span class="tv-label">Instruments:</span>
              <ElTag
                v-for="inst in step.instruments"
                :key="inst.instrument_id"
                size="small"
                type="info"
                class="tv-instrument-tag"
              >
                {{ inst.instrument_id }}
              </ElTag>
            </div>

            <!-- Measurements table-like list -->
            <div v-if="step.measurements.length > 0" class="tv-step-measurements">
              <div class="tv-meas-header">
                <span class="tv-meas-col-name">Measurement</span>
                <span class="tv-meas-col-value">Value</span>
                <span class="tv-meas-col-outcome">Outcome</span>
                <span class="tv-meas-col-time">Timestamp</span>
              </div>
              <div
                v-for="m in step.measurements"
                :key="m.measurement_id"
                class="tv-meas-row"
                data-testid="measurement-row"
              >
                <span class="tv-meas-col-name" :title="m.measurement_id">{{ m.name }}</span>
                <span class="tv-meas-col-value">{{ formatMeasurement(m) }}</span>
                <span class="tv-meas-col-outcome">
                  <ElTag size="small" :type="outcomeTagType(m.outcome)">{{ m.outcome }}</ElTag>
                </span>
                <span class="tv-meas-col-time">{{ formatDateTime(m.timestamp) }}</span>
              </div>
            </div>
            <div v-else class="tv-no-measurements">No measurements recorded for this execution.</div>
          </div>
        </ElTimelineItem>
      </ElTimeline>

      <ElEmpty
        v-else
        data-testid="empty-trace"
        description="No executions or measurements found for this serial number."
      />
    </ElCard>

    <!-- ─── JSON-LD collapsible viewer ─── -->
    <ElCollapse
      v-if="trace && showJsonld"
      :model-value="showJsonld ? ['jsonld'] : []"
      data-testid="jsonld-collapse"
    >
      <ElCollapseItem title="W3C PROV JSON-LD" name="jsonld">
        <pre class="tv-jsonld-pre" data-testid="jsonld-pre">{{ JSON.stringify(jsonld, null, 2) }}</pre>
      </ElCollapseItem>
    </ElCollapse>
  </div>
</template>

<style scoped>
.tracing-viewer {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  padding: var(--spacing-md) var(--spacing-lg);
  min-height: 100vh;
  background-color: var(--color-bg-secondary);
}

/* ─── Header ─── */
.tv-header {
  display: flex;
  align-items: center;
  gap: var(--spacing-md);
  flex-wrap: wrap;
}

.tv-title {
  font-size: 1.25rem;
  font-weight: 600;
  color: var(--color-text-primary);
  margin: 0;
}

.tv-search {
  flex: 1;
  min-width: 280px;
  max-width: 600px;
}

.tv-search-input {
  width: 100%;
}

/* ─── Summary badges ─── */
.tv-summary {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
  flex-wrap: wrap;
}

/* ─── Timeline card ─── */
.tv-timeline-card {
  background-color: var(--color-bg-primary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-xl);
}

.tv-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-sm);
}

.tv-card-title {
  font-size: 0.9375rem;
  font-weight: 600;
  color: var(--color-text-primary);
}

/* ─── Step ─── */
.tv-step {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
  padding: var(--spacing-xs) 0;
}

.tv-step-header {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  flex-wrap: wrap;
}

.tv-step-exec {
  font-weight: 600;
  color: var(--color-text-primary);
}

.tv-step-station,
.tv-step-seq,
.tv-step-duration {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
}

.tv-step-instruments {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
  flex-wrap: wrap;
}

.tv-label {
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
  font-weight: 500;
}

.tv-instrument-tag {
  font-family: monospace;
}

/* ─── Measurements table-like layout ─── */
.tv-step-measurements {
  display: flex;
  flex-direction: column;
  gap: 1px;
  background-color: var(--color-border-default);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-sm);
  overflow: hidden;
}

.tv-meas-header,
.tv-meas-row {
  display: grid;
  grid-template-columns: 1.5fr 1.5fr 0.8fr 1.2fr;
  gap: var(--spacing-xs);
  padding: var(--spacing-xs) var(--spacing-sm);
  background-color: var(--color-bg-primary);
}

.tv-meas-header {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--color-text-secondary);
  text-transform: uppercase;
  background-color: var(--color-bg-secondary);
}

.tv-meas-row:hover {
  background-color: var(--color-bg-secondary);
}

.tv-meas-col-name {
  font-family: monospace;
  font-size: 0.8125rem;
  color: var(--color-text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tv-meas-col-value {
  font-family: monospace;
  font-size: 0.8125rem;
  color: var(--color-text-primary);
}

.tv-meas-col-outcome {
  display: flex;
  align-items: center;
}

.tv-meas-col-time {
  font-size: 0.75rem;
  color: var(--color-text-secondary);
}

.tv-no-measurements {
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
  font-style: italic;
}

/* ─── JSON-LD viewer ─── */
.tv-jsonld-pre {
  background-color: var(--color-bg-secondary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-sm);
  padding: var(--spacing-sm);
  font-family: monospace;
  font-size: 0.75rem;
  color: var(--color-text-primary);
  overflow-x: auto;
  max-height: 400px;
  overflow-y: auto;
  margin: 0;
}

/* ─── Responsive ─── */
@media (max-width: 768px) {
  .tracing-viewer {
    padding: var(--spacing-sm);
  }

  .tv-meas-header,
  .tv-meas-row {
    grid-template-columns: 1fr 1fr;
    gap: 4px;
  }

  .tv-meas-col-time {
    grid-column: span 2;
  }
}
</style>
