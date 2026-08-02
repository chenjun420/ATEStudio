<script setup lang="ts">
/**
 * Operator Interaction Panel (T46).
 *
 * The core panel for the read-only operator mode. Combines:
 *
 * - **el-steps** step indicator showing the full test sequence with
 *   the current step highlighted.
 * - **el-card** displaying the current test name, work instruction,
 *   parameters, and expected values.
 * - **Instrument status tags** (el-tag with color coding) showing the
 *   health of resources used by the current step.
 * - **Scanner input** (el-input with auto-focus + Enter handling) for
 *   barcode scanner integration.
 * - **Quick action buttons**: Pass / Fail / Skip / Retry / Abort.
 * - **AI diagnosis suggestion dialog** (el-dialog) that appears when a
 *   step fails, showing root cause, confidence, possible causes, and
 *   repair steps.
 *
 * This component is designed for a read-only operator route
 * (`/#/operator/:station_id`). It does not allow editing sequences.
 */
import { computed, watch, onMounted, ref, nextTick } from 'vue'
import {
  ElSteps,
  ElStep,
  ElCard,
  ElDescriptions,
  ElDescriptionsItem,
  ElTag,
  ElButton,
  ElInput,
  ElDialog,
  ElAlert,
  ElEmpty,
  ElProgress,
  ElSkeleton,
  ElDivider,
  ElTooltip,
  ElIcon,
} from 'element-plus'
import { useOperatorInteraction } from '@/composables/useOperatorInteraction'
import type { OperatorAction } from '@/composables/useOperatorInteraction'
import type { OperatorStep, ResourceHealth } from '@/composables/useOperatorGuidance'
import type { StepStatus } from '@/composables/useExecutionStatus'

// ─── Props ───────────────────────────────────────────────────────────────────

interface Props {
  /** Station identifier from the route param. */
  stationId?: string
  /** Optional initial run ID (e.g., navigated from a run-link). */
  runId?: string
}

const props = withDefaults(defineProps<Props>(), {
  stationId: '',
  runId: '',
})

// ─── Emits ───────────────────────────────────────────────────────────────────

const emit = defineEmits<{
  (e: 'action', action: OperatorAction, stepId: string | null): void
}>()

// ─── Composable wiring ───────────────────────────────────────────────────────

const {
  stationId,
  runId,
  steps,
  currentStepIndex,
  currentStep,
  totalSteps,
  stepStatuses,
  executionStatus,
  isRunning,
  progressText,
  completedSteps,
  latestAlarm,
  connectionStatus,
  diagnosis,
  diagnosisLoading,
  diagnosisError,
  fetchDiagnosis,
  diagnosisDialogVisible,
  resourceHealth,
  sequenceLoading,
  sequenceError,
  scannerInput,
  handleScanSubmit,
  pendingCheckpoint,
  submitAction,
  actionLoading,
  actionError,
  actionLog,
  startRun,
  reset,
} = useOperatorInteraction(props.stationId, props.runId)

// ─── Run ID input (manual entry for testing) ─────────────────────────────────

const runIdInput = ref('')
const scannerInputRef = ref<InstanceType<typeof ElInput> | null>(null)

function handleStartRun(): void {
  const id = runIdInput.value.trim()
  if (id) {
    startRun(id)
  }
}

// ─── Auto-focus scanner input ────────────────────────────────────────────────

/**
 * Auto-focus the scanner input whenever:
 * - A scan/manual_input checkpoint is pending
 * - The component mounts
 * - A new run starts
 */
async function focusScanner(): Promise<void> {
  await nextTick()
  const inputEl = scannerInputRef.value?.$el?.querySelector('input')
  if (inputEl instanceof HTMLInputElement) {
    inputEl.focus()
  }
}

onMounted(() => {
  if (props.runId) {
    startRun(props.runId)
  }
  void focusScanner()
})

// Re-focus when a scan checkpoint becomes pending.
watch(
  pendingCheckpoint,
  (cp) => {
    if (
      cp?.pending &&
      cp.checkpoint &&
      (cp.checkpoint.type === 'scan' || cp.checkpoint.type === 'manual_input')
    ) {
      void focusScanner()
    }
  },
)

// ─── Step status -> Element Plus tag type mapping ────────────────────────────

function statusTagType(
  status: StepStatus,
): 'success' | 'warning' | 'danger' | 'info' | 'primary' {
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

function stepElStatus(
  status: StepStatus,
): 'wait' | 'process' | 'finish' | 'error' | 'success' {
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

function healthTagType(
  status: ResourceHealth['status'],
): 'success' | 'warning' | 'danger' | 'info' {
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

// ─── Quick action handler ────────────────────────────────────────────────────

/**
 * Handle a quick-action button click. Delegates to the composable's
 * `submitAction` and emits an event for the parent view.
 */
async function handleAction(action: OperatorAction): Promise<void> {
  const stepId = currentStep.value?.id ?? null
  await submitAction(action)
  emit('action', action, stepId)
}

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

// ─── Pending checkpoint display ──────────────────────────────────────────────

const checkpointPrompt = computed(() => {
  const cp = pendingCheckpoint.value
  if (!cp?.pending || !cp.checkpoint) return null
  return cp.checkpoint.prompt
})

const checkpointType = computed(() => {
  const cp = pendingCheckpoint.value
  if (!cp?.pending || !cp.checkpoint) return null
  return cp.checkpoint.type
})

// ─── Alarm banner ────────────────────────────────────────────────────────────

const alarmVisible = computed(() => latestAlarm.value !== null)

function dismissAlarm(): void {
  latestAlarm.value = null
}

// ─── Action log (latest 5 entries) ───────────────────────────────────────────

const recentActionLog = computed(() => actionLog.value.slice(-5).reverse())
</script>

<template>
  <div class="operator-interaction-panel">
    <!-- ─── Header bar ─── -->
    <header class="oip-header">
      <div class="oip-header-left">
        <h1 class="oip-title">Operator Station</h1>
        <ElTag v-if="stationId" type="info" size="small" data-testid="station-badge">
          Station: {{ stationId }}
        </ElTag>
      </div>
      <div class="oip-header-right">
        <!-- Connection indicator -->
        <div class="oip-conn" data-testid="connection-indicator">
          <span class="oip-conn-dot" :class="connectionDotClass()"></span>
          <span class="oip-conn-text">{{ connectionStatus }}</span>
        </div>
        <!-- Run ID input -->
        <div class="oip-run-input">
          <input
            v-model="runIdInput"
            type="text"
            placeholder="Enter run ID..."
            class="oip-input"
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

    <!-- ─── Action error banner ─── -->
    <ElAlert
      v-if="actionError"
      data-testid="action-error"
      title="Action Failed"
      :description="actionError"
      type="error"
      :closable="true"
      show-icon
      @close="actionError = null"
    />

    <!-- ─── Main content ─── -->
    <div class="oip-content">
      <!-- Loading skeleton -->
      <ElSkeleton v-if="sequenceLoading" :rows="6" animated data-testid="sequence-skeleton" />

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

      <!-- Steps + interaction ─── -->
      <template v-else>
        <!-- Progress -->
        <div class="oip-progress" data-testid="progress-bar">
          <ElProgress
            :percentage="totalSteps > 0 ? Math.round((completedSteps / totalSteps) * 100) : 0"
            :status="executionStatus === 'FAILED' ? 'exception' : executionStatus === 'COMPLETED' ? 'success' : undefined"
          />
          <span class="oip-progress-text">{{ progressText || `${completedSteps}/${totalSteps} steps` }}</span>
        </div>

        <!-- Step indicator (el-steps) -->
        <ElSteps
          :active="currentStepIndex"
          :status="executionStatus === 'FAILED' ? 'error' : executionStatus === 'COMPLETED' ? 'success' : 'process'"
          finish-status="success"
          align-center
          class="oip-steps"
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

        <!-- ─── Two-panel layout: step details + actions ─── -->
        <div class="oip-panels">
          <!-- ═══ LEFT: Current step details ═══ -->
          <section class="oip-panel oip-panel-left" data-testid="left-panel">
            <h2 class="oip-panel-title">Current Test Step</h2>

            <!-- Current step card -->
            <ElCard v-if="currentStep" class="oip-step-card" data-testid="current-step-card" shadow="hover">
              <template #header>
                <div class="oip-card-header">
                  <span class="oip-card-title">{{ currentStep.script }}</span>
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
                    class="oip-resource-tag"
                  >
                    {{ r }}
                  </ElTag>
                  <span v-if="currentStep.resources.length === 0" class="oip-muted">—</span>
                </ElDescriptionsItem>
                <ElDescriptionsItem label="Preconditions">
                  <ul v-if="currentStep.preconditions.length > 0" class="oip-pre-list">
                    <li v-for="(pc, i) in currentStep.preconditions" :key="i">{{ pc }}</li>
                  </ul>
                  <span v-else class="oip-muted">—</span>
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
              <span v-else class="oip-muted">No parameters</span>
            </ElCard>

            <!-- Instrument status tags -->
            <ElCard class="oip-health-card" shadow="never" data-testid="health-card">
              <template #header>
                <span class="oip-card-title">Instrument Status</span>
              </template>

              <div v-if="resourceHealth.length === 0" class="oip-muted" data-testid="no-resources">
                No resources declared in the current sequence.
              </div>

              <div v-else class="oip-health-list" data-testid="resource-health-list">
                <div v-for="r in resourceHealth" :key="r.name" class="oip-health-row">
                  <span class="oip-health-name">{{ r.name }}</span>
                  <span class="oip-health-type">{{ r.type }}</span>
                  <ElTag :type="healthTagType(r.status)" size="small">{{ r.status }}</ElTag>
                  <span v-if="r.detail" class="oip-health-detail">{{ r.detail }}</span>
                </div>
              </div>
            </ElCard>

            <!-- Action log -->
            <ElCard v-if="recentActionLog.length > 0" class="oip-log-card" shadow="never" data-testid="action-log-card">
              <template #header>
                <span class="oip-card-title">Recent Actions</span>
              </template>
              <div class="oip-log-list">
                <div v-for="(log, i) in recentActionLog" :key="i" class="oip-log-row">
                  <ElTag
                    :type="log.action === 'pass' ? 'success' : log.action === 'fail' ? 'danger' : log.action === 'abort' ? 'danger' : 'info'"
                    size="small"
                  >
                    {{ log.action.toUpperCase() }}
                  </ElTag>
                  <span class="oip-log-detail">{{ log.detail }}</span>
                  <span class="oip-log-time">{{ log.timestamp }}</span>
                </div>
              </div>
            </ElCard>
          </section>

          <!-- ═══ RIGHT: Scanner + Actions + AI ═══ -->
          <section class="oip-panel oip-panel-right" data-testid="right-panel">
            <!-- Pending checkpoint prompt -->
            <ElCard v-if="checkpointPrompt" class="oip-checkpoint-card" shadow="hover" data-testid="checkpoint-card">
              <template #header>
                <div class="oip-card-header">
                  <span class="oip-card-title">Checkpoint: {{ checkpointType }}</span>
                  <ElTag type="warning" size="small">Pending</ElTag>
                </div>
              </template>
              <p class="oip-checkpoint-prompt">{{ checkpointPrompt }}</p>
            </ElCard>

            <!-- Scanner input -->
            <ElCard class="oip-scanner-card" shadow="hover" data-testid="scanner-card">
              <template #header>
                <span class="oip-card-title">Barcode Scanner</span>
              </template>
              <ElInput
                ref="scannerInputRef"
                v-model="scannerInput"
                placeholder="Scan or enter barcode..."
                :disabled="actionLoading"
                clearable
                data-testid="scanner-input"
                @keyup.enter="handleScanSubmit"
              >
                <template #append>
                  <ElButton :loading="actionLoading" @click="handleScanSubmit" data-testid="scanner-submit">
                    Submit
                  </ElButton>
                </template>
              </ElInput>
              <p class="oip-scanner-hint">Auto-focused. Scanner input ends with Enter.</p>
            </ElCard>

            <!-- Quick action buttons -->
            <ElCard class="oip-actions-card" shadow="never" data-testid="actions-card">
              <template #header>
                <span class="oip-card-title">Quick Actions</span>
              </template>
              <div class="oip-actions">
                <ElButton
                  type="success"
                  :disabled="actionLoading || !isRunning"
                  :loading="actionLoading"
                  @click="handleAction('pass')"
                  data-testid="action-pass"
                >
                  Pass
                </ElButton>
                <ElButton
                  type="danger"
                  :disabled="actionLoading || !isRunning"
                  :loading="actionLoading"
                  @click="handleAction('fail')"
                  data-testid="action-fail"
                >
                  Fail
                </ElButton>
                <ElButton
                  :disabled="actionLoading || !isRunning"
                  @click="handleAction('skip')"
                  data-testid="action-skip"
                >
                  Skip
                </ElButton>
                <ElButton
                  :disabled="actionLoading || !isRunning"
                  @click="handleAction('retry')"
                  data-testid="action-retry"
                >
                  Retry
                </ElButton>
                <ElButton
                  type="danger"
                  plain
                  :disabled="actionLoading || !isRunning"
                  @click="handleAction('abort')"
                  data-testid="action-abort"
                >
                  Abort
                </ElButton>
              </div>
            </ElCard>

            <!-- AI diagnosis trigger -->
            <ElCard class="oip-diagnosis-trigger-card" shadow="never" data-testid="diagnosis-trigger-card">
              <template #header>
                <span class="oip-card-title">AI Diagnosis</span>
              </template>
              <ElButton
                v-if="runId"
                size="small"
                type="primary"
                :loading="diagnosisLoading"
                @click="() => { void fetchDiagnosis(currentStep?.id ?? null); diagnosisDialogVisible = true }"
                data-testid="open-diagnosis-btn"
              >
                View AI Diagnosis
              </ElButton>
              <span v-else class="oip-muted">Start a run to enable AI diagnosis.</span>
            </ElCard>
          </section>
        </div>
      </template>
    </div>

    <!-- ─── AI Diagnosis Dialog ─── -->
    <ElDialog
      v-model="diagnosisDialogVisible"
      title="AI Diagnosis Suggestion"
      width="600px"
      data-testid="diagnosis-dialog"
    >
      <!-- Loading -->
      <ElSkeleton v-if="diagnosisLoading" :rows="4" animated />

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
        <div v-if="diagnosis.possible_causes.length > 0" class="oip-subsection" data-testid="possible-causes">
          <h4 class="oip-subsection-title">Possible Causes</h4>
          <ul class="oip-cause-list">
            <li v-for="(cause, i) in diagnosis.possible_causes" :key="i" class="oip-cause-item">
              <ElTooltip
                v-if="cause.description"
                :content="cause.description"
                placement="top"
              >
                <span class="oip-cause-label">{{ cause.label }}</span>
              </ElTooltip>
              <span v-else class="oip-cause-label">{{ cause.label }}</span>
              <ElTag size="small" :type="cause.confidence >= 0.5 ? 'warning' : 'info'">
                {{ confidencePercent(cause.confidence) }}%
              </ElTag>
            </li>
          </ul>
        </div>

        <!-- Repair steps -->
        <div v-if="diagnosis.repair_steps.length > 0" class="oip-subsection" data-testid="repair-steps">
          <h4 class="oip-subsection-title">Repair Suggestions</h4>
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
        <div v-if="diagnosis.notes" class="oip-subsection">
          <h4 class="oip-subsection-title">Notes</h4>
          <p class="oip-notes">{{ diagnosis.notes }}</p>
        </div>
      </div>

      <!-- Empty -->
      <ElEmpty
        v-else
        data-testid="diagnosis-empty"
        description="No diagnosis available. Diagnosis loads automatically when a step fails."
      />

      <template #footer>
        <ElButton @click="diagnosisDialogVisible = false">Close</ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.operator-interaction-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 100vh;
  background-color: var(--color-bg-secondary);
}

/* ─── Header ─── */
.oip-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--spacing-md) var(--spacing-lg);
  background-color: var(--color-bg-elevated);
  border-bottom: 1px solid var(--color-border-default);
  flex-wrap: wrap;
  gap: var(--spacing-sm);
}

.oip-header-left {
  display: flex;
  align-items: center;
  gap: var(--spacing-md);
}

.oip-header-right {
  display: flex;
  align-items: center;
  gap: var(--spacing-lg);
}

.oip-title {
  font-size: 1.25rem;
  font-weight: 600;
  color: var(--color-text-primary);
  margin: 0;
}

/* ─── Connection indicator ─── */
.oip-conn {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
}

.oip-conn-dot {
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
.oip-run-input {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
}

.oip-input {
  padding: var(--spacing-xs) var(--spacing-sm);
  font-size: 0.8125rem;
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-md);
  width: 180px;
  background-color: var(--color-bg-primary);
  color: var(--color-text-primary);
  transition: border-color var(--transition-fast);
}

.oip-input:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 2px var(--color-border-accent);
}

/* ─── Content ─── */
.oip-content {
  flex: 1;
  overflow-y: auto;
  padding: var(--spacing-md) var(--spacing-lg);
}

/* ─── Progress ─── */
.oip-progress {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  margin-bottom: var(--spacing-md);
}

.oip-progress :deep(.el-progress) {
  flex: 1;
}

.oip-progress-text {
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
  white-space: nowrap;
}

/* ─── Steps ─── */
.oip-steps {
  margin: var(--spacing-sm) 0 var(--spacing-md) 0;
}

/* ─── Two-panel layout ─── */
.oip-panels {
  display: flex;
  gap: var(--spacing-md);
}

.oip-panel {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.oip-panel-left {
  flex: 1.2;
}

.oip-panel-right {
  flex: 1;
  max-width: 420px;
}

.oip-panel-title {
  font-size: 1rem;
  font-weight: 600;
  color: var(--color-text-primary);
  margin: 0 0 var(--spacing-xs) 0;
  padding-bottom: var(--spacing-xs);
  border-bottom: 1px solid var(--color-border-muted);
}

/* ─── Cards ─── */
.oip-step-card,
.oip-health-card,
.oip-log-card,
.oip-checkpoint-card,
.oip-scanner-card,
.oip-actions-card,
.oip-diagnosis-trigger-card {
  margin-bottom: var(--spacing-sm);
}

.oip-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-sm);
}

.oip-card-title {
  font-size: 0.9375rem;
  font-weight: 600;
  color: var(--color-text-primary);
}

/* ─── Resource tags ─── */
.oip-resource-tag {
  margin-right: var(--spacing-xs);
  margin-bottom: 2px;
}

/* ─── Preconditions list ─── */
.oip-pre-list {
  margin: 0;
  padding-left: var(--spacing-lg);
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
}

.oip-muted {
  color: var(--color-text-tertiary);
  font-size: 0.8125rem;
}

/* ─── Resource health ─── */
.oip-health-list {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.oip-health-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  padding: var(--spacing-xs) var(--spacing-sm);
  border-radius: var(--radius-md);
  background-color: var(--color-bg-secondary);
}

.oip-health-name {
  flex: 1;
  font-size: 0.8125rem;
  font-weight: 500;
  color: var(--color-text-primary);
}

.oip-health-type {
  font-size: 0.75rem;
  color: var(--color-text-tertiary);
  text-transform: capitalize;
}

.oip-health-detail {
  font-size: 0.75rem;
  color: var(--color-warning);
}

/* ─── Action log ─── */
.oip-log-list {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.oip-log-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  padding: var(--spacing-xs) var(--spacing-sm);
  border-radius: var(--radius-md);
  background-color: var(--color-bg-secondary);
}

.oip-log-detail {
  flex: 1;
  font-size: 0.75rem;
  color: var(--color-text-secondary);
}

.oip-log-time {
  font-size: 0.6875rem;
  color: var(--color-text-tertiary);
  font-variant-numeric: tabular-nums;
}

/* ─── Checkpoint ─── */
.oip-checkpoint-prompt {
  font-size: 0.9375rem;
  color: var(--color-text-primary);
  margin: 0;
  font-weight: 500;
}

/* ─── Scanner ─── */
.oip-scanner-hint {
  font-size: 0.75rem;
  color: var(--color-text-tertiary);
  margin: var(--spacing-xs) 0 0 0;
}

/* ─── Actions ─── */
.oip-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-sm);
}

/* ─── Diagnosis subsections ─── */
.oip-subsection {
  margin-top: var(--spacing-md);
}

.oip-subsection-title {
  font-size: 0.8125rem;
  font-weight: 600;
  color: var(--color-text-secondary);
  margin: 0 0 var(--spacing-xs) 0;
  text-transform: uppercase;
  letter-spacing: 0.03em;
}

.oip-cause-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.oip-cause-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-sm);
  padding: var(--spacing-xs) var(--spacing-sm);
  background-color: var(--color-bg-secondary);
  border-radius: var(--radius-md);
}

.oip-cause-label {
  font-size: 0.8125rem;
  color: var(--color-text-primary);
}

.oip-notes {
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
  margin: 0;
  white-space: pre-wrap;
}

/* ─── Responsive ─── */
@media (max-width: 1024px) {
  .oip-panels {
    flex-direction: column;
  }
  .oip-panel-left,
  .oip-panel-right {
    flex: 1;
    max-width: none;
  }
}
</style>
