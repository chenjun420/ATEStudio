<script setup lang="ts">
/**
 * Operator Guidance UI.
 *
 * Two-panel layout for production-line operators:
 *   LEFT  - current test step (el-steps), work instruction, parameters,
 *           expected values (el-descriptions).
 *   RIGHT - AI diagnosis panel (el-card with possible causes + repair steps),
 *           instrument / resource health status (el-tag), and the latest
 *           alarm (el-alert).
 *
 * Real-time updates come from the existing SSE composable `useExecutionStatus`,
 * aggregated by `useOperatorGuidance` which also fetches the sequence YAML
 * and the (forthcoming) AI diagnosis.
 *
 * Route: /operator/:stationId?  (stationId is optional; defaults to empty)
 */
import { computed, watch, onMounted, ref } from 'vue'
import {
  ElSteps,
  ElStep,
  ElAlert,
  ElCard,
  ElDescriptions,
  ElDescriptionsItem,
  ElTag,
  ElButton,
  ElEmpty,
  ElProgress,
  ElSkeleton,
  ElDivider,
  ElTooltip,
} from 'element-plus'
import { useOperatorGuidance } from '@/composables/useOperatorGuidance'
import type { ResourceHealth } from '@/composables/useOperatorGuidance'
import type { StepStatus } from '@/composables/useExecutionStatus'

// ─── Props ───────────────────────────────────────────────────────────────────

interface Props {
  /** Station identifier from the route param. Optional. */
  stationId?: string
  /** Optional initial run ID (e.g., navigated from a run-link). */
  runId?: string
}

const props = withDefaults(defineProps<Props>(), {
  stationId: '',
  runId: '',
})

// ─── Composable wiring ───────────────────────────────────────────────────────

const {
  stationId,
  runId,
  steps,
  currentStepIndex,
  currentStep,
  totalSteps,
  executionStatus,
  progressText,
  completedSteps,
  latestAlarm,
  latestMeasurements,
  connectionStatus,
  diagnosis,
  diagnosisLoading,
  diagnosisError,
  fetchDiagnosis,
  resourceHealth,
  sequenceLoading,
  sequenceError,
  startRun,
  reset,
} = useOperatorGuidance(props.stationId, props.runId)

// ─── Run ID input (manual entry for testing) ─────────────────────────────────

const runIdInput = ref('')

function handleStartRun(): void {
  const id = runIdInput.value.trim()
  if (id) {
    startRun(id)
  }
}

// ─── Step status -> Element Plus tag type mapping ────────────────────────────

/**
 * Map a StepStatus to an Element Plus tag type for color coding.
 */
function statusTagType(status: StepStatus): 'success' | 'warning' | 'danger' | 'info' | 'primary' {
  switch (status) {
    case 'passed':
      return 'success'
    case 'failed':
      return 'danger'
    case 'error':
      return 'danger'
    case 'running':
      return 'primary'
    case 'skipped':
      return 'info'
    default:
      return 'info'
  }
}

/**
 * Human-readable status label.
 */
function statusLabel(status: StepStatus): string {
  const labels: Record<StepStatus, string> = {
    idle: 'Pending',
    running: 'Running',
    passed: 'Passed',
    failed: 'Failed',
    error: 'Error',
    skipped: 'Skipped',
  }
  return labels[status] ?? status
}

// ─── el-steps status mapping ─────────────────────────────────────────────────

/**
 * Map a StepStatus to el-step `status` prop value.
 * el-step accepts: 'wait' | 'process' | 'finish' | 'error' | 'success'.
 */
function stepElStatus(status: StepStatus): 'wait' | 'process' | 'finish' | 'error' | 'success' {
  switch (status) {
    case 'passed':
      return 'success'
    case 'failed':
    case 'error':
      return 'error'
    case 'running':
      return 'process'
    case 'skipped':
      return 'finish'
    default:
      return 'wait'
  }
}

// ─── Resource health -> tag type ─────────────────────────────────────────────

function healthTagType(status: ResourceHealth['status']): 'success' | 'warning' | 'danger' | 'info' {
  switch (status) {
    case 'healthy':
      return 'success'
    case 'degraded':
      return 'warning'
    case 'offline':
      return 'danger'
    default:
      return 'info'
  }
}

// ─── Confidence formatting ───────────────────────────────────────────────────

function confidencePercent(confidence: number): number {
  return Math.round(confidence * 100)
}

// ─── Auto-fetch diagnosis when a step fails ──────────────────────────────────

/**
 * When the current step transitions to failed/error, automatically fetch an
 * AI diagnosis for it so the operator sees repair suggestions immediately.
 */
watch(
  () => currentStep.value?.status,
  (newStatus) => {
    if (newStatus === 'failed' || newStatus === 'error') {
      const stepId = currentStep.value?.id ?? null
      void fetchDiagnosis(stepId)
    }
  },
)

// ─── Execution-level alarm alert ─────────────────────────────────────────────

const alarmVisible = computed(() => latestAlarm.value !== null)

function dismissAlarm(): void {
  // The composable doesn't expose a clear-alarm action; we hide locally.
  latestAlarm.value = null
}

// ─── Latest measurements as a flat list for display ──────────────────────────

const measurementList = computed(() => {
  return Object.entries(latestMeasurements).map(([name, event]) => ({
    name,
    value: event.value ?? event.measurement ?? '—',
    unit: (event.unit as string) ?? '',
    timestamp: (event.timestamp as string) ?? '',
  }))
})

// ─── Connection status indicator ─────────────────────────────────────────────

function connectionDotClass(): string {
  switch (connectionStatus.value) {
    case 'connected':
      return 'dot-connected'
    case 'connecting':
    case 'reconnecting':
      return 'dot-connecting'
    case 'error':
      return 'dot-error'
    default:
      return 'dot-disconnected'
  }
}

// ─── Mount: if a runId was provided via props, start it ──────────────────────

onMounted(() => {
  if (props.runId) {
    startRun(props.runId)
  }
})
</script>

<template>
  <div class="operator-guidance">
    <!-- ─── Header bar ─── -->
    <header class="og-header">
      <div class="og-header-left">
        <h1 class="og-title">Operator Guidance</h1>
        <ElTag v-if="stationId" type="info" size="small" data-testid="station-badge">
          Station: {{ stationId }}
        </ElTag>
      </div>
      <div class="og-header-right">
        <!-- Connection indicator -->
        <div class="og-conn" data-testid="connection-indicator">
          <span class="og-conn-dot" :class="connectionDotClass()"></span>
          <span class="og-conn-text">{{ connectionStatus }}</span>
        </div>
        <!-- Run ID input -->
        <div class="og-run-input">
          <input
            v-model="runIdInput"
            type="text"
            placeholder="Enter run ID..."
            class="og-input"
            data-testid="run-id-input"
            @keyup.enter="handleStartRun"
          />
          <ElButton type="primary" size="small" :disabled="!runIdInput.trim()" @click="handleStartRun">
            Start
          </ElButton>
          <ElButton size="small" :disabled="!runId" @click="reset">Reset</ElButton>
        </div>
      </div>
    </header>

    <!-- ─── Alarm banner ─── -->
    <ElAlert
      v-if="alarmVisible && latestAlarm"
      data-testid="alarm-alert"
      :title="latestAlarm.type"
      :description="String(latestAlarm.message ?? latestAlarm.step_id ?? 'Alarm triggered')"
      :type="latestAlarm.severity === 'critical' ? 'error' : 'warning'"
      :closable="true"
      show-icon
      @close="dismissAlarm"
    />

    <!-- ─── Two-panel layout ─── -->
    <div class="og-panels">
      <!-- ═══ LEFT PANEL: Steps + work instructions ═══ -->
      <section class="og-panel og-panel-left" data-testid="left-panel">
        <h2 class="og-panel-title">Test Sequence</h2>

        <!-- Loading skeleton -->
        <ElSkeleton v-if="sequenceLoading" :rows="4" animated data-testid="sequence-skeleton" />

        <!-- Sequence error -->
        <ElAlert
          v-else-if="sequenceError"
          data-testid="sequence-error"
          title="Failed to load sequence"
          :description="sequenceError"
          type="error"
          :closable="false"
          show-icon
        />

        <!-- Empty state -->
        <ElEmpty
          v-else-if="totalSteps === 0"
          data-testid="empty-steps"
          description="No sequence loaded. Enter a run ID to begin."
        />

        <!-- Steps + details -->
        <template v-else>
          <!-- Progress -->
          <div class="og-progress" data-testid="progress-bar">
            <ElProgress
              :percentage="totalSteps > 0 ? Math.round((completedSteps / totalSteps) * 100) : 0"
              :status="executionStatus === 'FAILED' ? 'exception' : executionStatus === 'COMPLETED' ? 'success' : undefined"
            />
            <span class="og-progress-text">{{ progressText || `${completedSteps}/${totalSteps} steps` }}</span>
          </div>

          <!-- Step indicator (el-steps) -->
          <ElSteps
            :active="currentStepIndex"
            :status="executionStatus === 'FAILED' ? 'error' : executionStatus === 'COMPLETED' ? 'success' : 'process'"
            finish-status="success"
            align-center
            class="og-steps"
            data-testid="step-indicator"
          >
            <ElStep
              v-for="step in steps"
              :key="step.id"
              :title="step.script"
              :description="step.id"
              :status="stepElStatus(step.status)"
            />
          </ElSteps>

          <!-- Current step details -->
          <ElCard v-if="currentStep" class="og-step-card" data-testid="current-step-card" shadow="hover">
            <template #header>
              <div class="og-card-header">
                <span class="og-card-title">{{ currentStep.script }}</span>
                <ElTag :type="statusTagType(currentStep.status)" size="small" data-testid="current-step-status">
                  {{ statusLabel(currentStep.status) }}
                </ElTag>
              </div>
            </template>

            <ElDescriptions :column="1" border size="small">
              <ElDescriptionsItem label="Step ID">{{ currentStep.id }}</ElDescriptionsItem>
              <ElDescriptionsItem label="Timeout">{{ currentStep.timeout_ms }} ms</ElDescriptionsItem>
              <ElDescriptionsItem label="Retry">{{ currentStep.retry }}</ElDescriptionsItem>
              <ElDescriptionsItem label="On Fail">{{ currentStep.on_fail }}</ElDescriptionsItem>
              <ElDescriptionsItem label="Resources">
                <ElTag
                  v-for="r in currentStep.resources"
                  :key="r"
                  size="small"
                  type="info"
                  class="og-resource-tag"
                >
                  {{ r }}
                </ElTag>
                <span v-if="currentStep.resources.length === 0" class="og-muted">—</span>
              </ElDescriptionsItem>
              <ElDescriptionsItem label="Preconditions">
                <ul v-if="currentStep.preconditions.length > 0" class="og-pre-list">
                  <li v-for="(pc, i) in currentStep.preconditions" :key="i">{{ pc }}</li>
                </ul>
                <span v-else class="og-muted">—</span>
              </ElDescriptionsItem>
            </ElDescriptions>

            <!-- Parameters (expected values) -->
            <ElDivider content-position="left">Parameters / Expected Values</ElDivider>
            <ElDescriptions v-if="Object.keys(currentStep.params).length > 0" :column="1" border size="small" data-testid="step-params">
              <ElDescriptionsItem
                v-for="(val, key) in currentStep.params"
                :key="String(key)"
                :label="String(key)"
              >
                {{ typeof val === 'object' ? JSON.stringify(val) : String(val) }}
              </ElDescriptionsItem>
            </ElDescriptions>
            <span v-else class="og-muted">No parameters</span>
          </ElCard>

          <!-- All steps table-like list with status tags -->
          <ElCard class="og-all-steps" shadow="never" data-testid="all-steps-card">
            <template #header>
              <span class="og-card-title">All Steps ({{ totalSteps }})</span>
            </template>
            <div class="og-step-list">
              <div
                v-for="(step, idx) in steps"
                :key="step.id"
                class="og-step-row"
                :class="{ 'og-step-row-active': idx === currentStepIndex }"
              >
                <span class="og-step-idx">{{ idx + 1 }}</span>
                <span class="og-step-name">{{ step.script }}</span>
                <ElTag :type="statusTagType(step.status)" size="small">
                  {{ statusLabel(step.status) }}
                </ElTag>
              </div>
            </div>
          </ElCard>
        </template>
      </section>

      <!-- ═══ RIGHT PANEL: AI diagnosis + resource health ═══ -->
      <section class="og-panel og-panel-right" data-testid="right-panel">
        <h2 class="og-panel-title">AI Diagnosis &amp; Health</h2>

        <!-- AI Diagnosis card -->
        <ElCard class="og-diagnosis-card" shadow="hover" data-testid="diagnosis-card">
          <template #header>
            <div class="og-card-header">
              <span class="og-card-title">AI Diagnosis</span>
              <ElButton
                v-if="runId"
                size="small"
                type="primary"
                :loading="diagnosisLoading"
                @click="fetchDiagnosis(currentStep?.id ?? null)"
                data-testid="fetch-diagnosis-btn"
              >
                Refresh
              </ElButton>
            </div>
          </template>

          <!-- Loading -->
          <ElSkeleton v-if="diagnosisLoading" :rows="3" animated />

          <!-- Error -->
          <ElAlert
            v-else-if="diagnosisError"
            data-testid="diagnosis-error"
            title="Diagnosis unavailable"
            :description="diagnosisError"
            type="warning"
            :closable="false"
            show-icon
          />

          <!-- Result -->
          <div v-else-if="diagnosis" data-testid="diagnosis-result">
            <ElDescriptions :column="1" border size="small">
              <ElDescriptionsItem label="Root Cause">
                <strong>{{ diagnosis.root_cause }}</strong>
              </ElDescriptionsItem>
              <ElDescriptionsItem label="Confidence">
                <ElProgress
                  :percentage="confidencePercent(diagnosis.confidence)"
                  :stroke-width="14"
                  :status="diagnosis.confidence >= 0.7 ? 'success' : diagnosis.confidence >= 0.4 ? undefined : 'exception'"
                />
              </ElDescriptionsItem>
              <ElDescriptionsItem v-if="diagnosis.step_id" label="Step">{{ diagnosis.step_id }}</ElDescriptionsItem>
            </ElDescriptions>

            <!-- Possible causes -->
            <div v-if="diagnosis.possible_causes.length > 0" class="og-subsection" data-testid="possible-causes">
              <h4 class="og-subsection-title">Possible Causes</h4>
              <ul class="og-cause-list">
                <li v-for="(cause, i) in diagnosis.possible_causes" :key="i" class="og-cause-item">
                  <ElTooltip
                    v-if="cause.description"
                    :content="cause.description"
                    placement="top"
                  >
                    <span class="og-cause-label">{{ cause.label }}</span>
                  </ElTooltip>
                  <span v-else class="og-cause-label">{{ cause.label }}</span>
                  <ElTag size="small" :type="cause.confidence >= 0.5 ? 'warning' : 'info'">
                    {{ confidencePercent(cause.confidence) }}%
                  </ElTag>
                </li>
              </ul>
            </div>

            <!-- Repair steps -->
            <div v-if="diagnosis.repair_steps.length > 0" class="og-subsection" data-testid="repair-steps">
              <h4 class="og-subsection-title">Repair Suggestions</h4>
              <ElSteps direction="vertical" :active="0" process-status="process">
                <ElStep
                  v-for="rs in diagnosis.repair_steps"
                  :key="rs.order"
                  :title="`Step ${rs.order}`"
                  :description="rs.action + (rs.estimated_seconds ? ` (~${rs.estimated_seconds}s)` : '')"
                />
              </ElSteps>
            </div>

            <!-- Notes -->
            <div v-if="diagnosis.notes" class="og-subsection">
              <h4 class="og-subsection-title">Notes</h4>
              <p class="og-notes">{{ diagnosis.notes }}</p>
            </div>
          </div>

          <!-- Empty -->
          <ElEmpty
            v-else
            data-testid="diagnosis-empty"
            description="No diagnosis yet. Diagnosis loads automatically when a step fails, or click Refresh."
          />
        </ElCard>

        <!-- Resource / instrument health -->
        <ElCard class="og-health-card" shadow="never" data-testid="health-card">
          <template #header>
            <span class="og-card-title">Instrument &amp; Resource Health</span>
          </template>

          <div v-if="resourceHealth.length === 0" class="og-muted" data-testid="no-resources">
            No resources declared in the current sequence.
          </div>

          <div v-else class="og-health-list" data-testid="resource-health-list">
            <div v-for="r in resourceHealth" :key="r.name" class="og-health-row">
              <span class="og-health-name">{{ r.name }}</span>
              <span class="og-health-type">{{ r.type }}</span>
              <ElTag :type="healthTagType(r.status)" size="small">{{ r.status }}</ElTag>
              <span v-if="r.detail" class="og-health-detail">{{ r.detail }}</span>
            </div>
          </div>
        </ElCard>

        <!-- Latest measurements -->
        <ElCard v-if="measurementList.length > 0" class="og-measure-card" shadow="never" data-testid="measurements-card">
          <template #header>
            <span class="og-card-title">Latest Measurements</span>
          </template>
          <div class="og-measure-list">
            <div v-for="m in measurementList" :key="m.name" class="og-measure-row">
              <span class="og-measure-name">{{ m.name }}</span>
              <span class="og-measure-value">{{ m.value }}{{ m.unit ? ' ' + m.unit : '' }}</span>
            </div>
          </div>
        </ElCard>
      </section>
    </div>
  </div>
</template>

<style scoped>
.operator-guidance {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 100vh;
  background-color: var(--color-bg-secondary);
}

/* ─── Header ─── */
.og-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--spacing-md) var(--spacing-lg);
  background-color: var(--color-bg-elevated);
  border-bottom: 1px solid var(--color-border-default);
  flex-wrap: wrap;
  gap: var(--spacing-sm);
}

.og-header-left {
  display: flex;
  align-items: center;
  gap: var(--spacing-md);
}

.og-header-right {
  display: flex;
  align-items: center;
  gap: var(--spacing-lg);
}

.og-title {
  font-size: 1.25rem;
  font-weight: 600;
  color: var(--color-text-primary);
  margin: 0;
}

/* ─── Connection indicator ─── */
.og-conn {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
}

.og-conn-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}

.dot-connected {
  background-color: var(--color-success);
}
.dot-connecting {
  background-color: var(--color-warning);
  animation: pulse 1.5s infinite;
}
.dot-error {
  background-color: var(--color-error);
}
.dot-disconnected {
  background-color: var(--color-text-tertiary);
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

/* ─── Run ID input ─── */
.og-run-input {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
}

.og-input {
  padding: var(--spacing-xs) var(--spacing-sm);
  font-size: 0.8125rem;
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-md);
  width: 180px;
  background-color: var(--color-bg-primary);
  color: var(--color-text-primary);
  transition: border-color var(--transition-fast);
}

.og-input:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 2px var(--color-border-accent);
}

/* ─── Two-panel layout ─── */
.og-panels {
  display: flex;
  flex: 1;
  gap: var(--spacing-md);
  padding: var(--spacing-md) var(--spacing-lg);
  overflow: hidden;
}

.og-panel {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  overflow-y: auto;
  padding: var(--spacing-md);
  background-color: var(--color-bg-primary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-xl);
}

.og-panel-left {
  flex: 1.2;
}

.og-panel-right {
  flex: 1;
}

.og-panel-title {
  font-size: 1rem;
  font-weight: 600;
  color: var(--color-text-primary);
  margin: 0 0 var(--spacing-xs) 0;
  padding-bottom: var(--spacing-xs);
  border-bottom: 1px solid var(--color-border-muted);
}

/* ─── Progress ─── */
.og-progress {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
}

.og-progress :deep(.el-progress) {
  flex: 1;
}

.og-progress-text {
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
  white-space: nowrap;
}

/* ─── Steps ─── */
.og-steps {
  margin: var(--spacing-sm) 0;
}

/* ─── Cards ─── */
.og-step-card,
.og-all-steps,
.og-diagnosis-card,
.og-health-card,
.og-measure-card {
  margin-bottom: var(--spacing-sm);
}

.og-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-sm);
}

.og-card-title {
  font-size: 0.9375rem;
  font-weight: 600;
  color: var(--color-text-primary);
}

/* ─── Resource tags ─── */
.og-resource-tag {
  margin-right: var(--spacing-xs);
  margin-bottom: 2px;
}

/* ─── Preconditions list ─── */
.og-pre-list {
  margin: 0;
  padding-left: var(--spacing-lg);
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
}

.og-muted {
  color: var(--color-text-tertiary);
  font-size: 0.8125rem;
}

/* ─── All steps list ─── */
.og-step-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.og-step-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  padding: var(--spacing-xs) var(--spacing-sm);
  border-radius: var(--radius-md);
  transition: background-color var(--transition-fast);
}

.og-step-row:hover {
  background-color: var(--color-bg-tertiary);
}

.og-step-row-active {
  background-color: var(--color-border-accent);
}

.og-step-idx {
  width: 24px;
  text-align: right;
  font-size: 0.75rem;
  color: var(--color-text-tertiary);
  font-variant-numeric: tabular-nums;
}

.og-step-name {
  flex: 1;
  font-size: 0.8125rem;
  color: var(--color-text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ─── Diagnosis subsections ─── */
.og-subsection {
  margin-top: var(--spacing-md);
}

.og-subsection-title {
  font-size: 0.8125rem;
  font-weight: 600;
  color: var(--color-text-secondary);
  margin: 0 0 var(--spacing-xs) 0;
  text-transform: uppercase;
  letter-spacing: 0.03em;
}

.og-cause-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.og-cause-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-sm);
  padding: var(--spacing-xs) var(--spacing-sm);
  background-color: var(--color-bg-secondary);
  border-radius: var(--radius-md);
}

.og-cause-label {
  font-size: 0.8125rem;
  color: var(--color-text-primary);
}

.og-notes {
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
  margin: 0;
  white-space: pre-wrap;
}

/* ─── Resource health ─── */
.og-health-list {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.og-health-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  padding: var(--spacing-xs) var(--spacing-sm);
  border-radius: var(--radius-md);
  background-color: var(--color-bg-secondary);
}

.og-health-name {
  flex: 1;
  font-size: 0.8125rem;
  font-weight: 500;
  color: var(--color-text-primary);
}

.og-health-type {
  font-size: 0.75rem;
  color: var(--color-text-tertiary);
  text-transform: capitalize;
}

.og-health-detail {
  font-size: 0.75rem;
  color: var(--color-warning);
}

/* ─── Measurements ─── */
.og-measure-list {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.og-measure-row {
  display: flex;
  justify-content: space-between;
  padding: var(--spacing-xs) var(--spacing-sm);
  border-radius: var(--radius-md);
  background-color: var(--color-bg-secondary);
}

.og-measure-name {
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
}

.og-measure-value {
  font-size: 0.8125rem;
  font-weight: 600;
  color: var(--color-text-primary);
  font-variant-numeric: tabular-nums;
}

/* ─── Responsive ─── */
@media (max-width: 1024px) {
  .og-panels {
    flex-direction: column;
  }
  .og-panel-left,
  .og-panel-right {
    flex: 1;
  }
}
</style>
