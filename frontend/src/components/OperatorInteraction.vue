<script setup lang="ts">
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { ElDialog, ElInput, ElButton, ElTag } from 'element-plus'
import http from '@/api/interceptor'

/**
 * OperatorInteraction - modal dialog for operator checkpoints.
 *
 * Listens for pending operator checkpoints on a given execution run
 * (polls ``GET /api/v1/executions/{run_id}/checkpoint/pending`` at a
 * fixed interval). When a pending checkpoint is found, opens an
 * ``el-dialog`` tailored to the checkpoint ``type``:
 *
 * - ``scan``: ``el-input`` autofocus, expects a barcode string. Enter
 *   submits the response.
 * - ``manual_input``: ``el-input`` (no autofocus behaviour change),
 *   operator types free text and clicks Submit.
 * - ``visual_check``: two ``el-button``s (Pass / Fail). Fail prompts
 *   for an optional reason before submitting.
 * - ``confirm``: a single ``el-button`` (Confirm).
 *
 * The operator's response is submitted via
 * ``POST /api/v1/executions/{run_id}/checkpoint``. After submission the
 * dialog closes and polling resumes until the parent component unmounts
 * or sets ``runId`` to null.
 *
 * The component is intentionally self-contained: it manages its own
 * polling loop and HTTP calls, emitting ``resolved`` and ``failed``
 * events for the parent to react to (e.g. update execution status).
 */

interface OperatorCheckpoint {
  type: 'scan' | 'manual_input' | 'visual_check' | 'confirm'
  prompt: string
  timeout_sec: number
  validation_regex?: string | null
}

interface PendingCheckpointResponse {
  run_id: string
  pending: boolean
  step_id?: string | null
  checkpoint?: OperatorCheckpoint | null
  created_at?: string | null
}

interface Props {
  /** Execution run identifier to watch. Null disables polling. */
  runId: string | null
  /** Polling interval in ms (default 2000). */
  pollIntervalMs?: number
}

const props = withDefaults(defineProps<Props>(), {
  pollIntervalMs: 2000,
})

const emit = defineEmits<{
  /** Emitted when the operator submits a response (any type). */
  resolved: [stepId: string, response: string]
  /** Emitted when a visual_check fails (response="fail"). */
  failed: [stepId: string, reason: string]
  /** Emitted when the dialog is dismissed without a submission. */
  cancelled: [stepId: string]
}>()

// --- State ---------------------------------------------------------------

const visible = ref(false)
const submitting = ref(false)
const inputValue = ref('')
const failReason = ref('')
const showFailReason = ref(false)
const errorMessage = ref('')
const secondsLeft = ref(0)

const pending = ref<PendingCheckpointResponse | null>(null)
let pollTimer: ReturnType<typeof setInterval> | null = null
let countdownTimer: ReturnType<typeof setInterval> | null = null

// --- Computed ------------------------------------------------------------

const checkpoint = computed<OperatorCheckpoint | null>(
  () => pending.value?.checkpoint ?? null,
)
const stepId = computed<string>(() => pending.value?.step_id ?? '')
const checkpointType = computed<string>(() => checkpoint.value?.type ?? '')

const dialogTitle = computed<string>(() => {
  switch (checkpointType.value) {
    case 'scan':
      return 'Scan Required'
    case 'manual_input':
      return 'Operator Input Required'
    case 'visual_check':
      return 'Visual Check Required'
    case 'confirm':
      return 'Confirmation Required'
    default:
      return 'Operator Checkpoint'
  }
})

const inputPlaceholder = computed<string>(() => {
  if (checkpointType.value === 'scan') return 'Scan barcode here...'
  if (checkpointType.value === 'manual_input') return 'Enter value...'
  return ''
})

// --- Validation ----------------------------------------------------------

function isValidInput(): boolean {
  if (!checkpoint.value) return false
  if (checkpointType.value === 'visual_check' || checkpointType.value === 'confirm') {
    return true
  }
  const val = inputValue.value.trim()
  if (!val) return false
  const regex = checkpoint.value.validation_regex
  if (regex) {
    try {
      return new RegExp(regex).test(val)
    } catch {
      return true // Invalid regex pattern - don't block the operator
    }
  }
  return true
}

// --- Polling -------------------------------------------------------------

async function pollPending(): Promise<void> {
  if (!props.runId || visible.value || submitting.value) return
  try {
    const res = await http.get<PendingCheckpointResponse>(
      `/executions/${props.runId}/checkpoint/pending`,
    )
    if (res.data.pending && res.data.checkpoint && res.data.step_id) {
      pending.value = res.data
      inputValue.value = ''
      failReason.value = ''
      showFailReason.value = false
      errorMessage.value = ''
      visible.value = true
      startCountdown()
    }
  } catch (err) {
    // Network errors are non-fatal - the next poll will retry.
    console.error('Failed to poll pending checkpoint:', err)
  }
}

function startPolling(): void {
  stopPolling()
  pollTimer = setInterval(pollPending, props.pollIntervalMs)
  // Fire immediately so the dialog opens without waiting for the first tick.
  void pollPending()
}

function stopPolling(): void {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

// --- Countdown -----------------------------------------------------------

function startCountdown(): void {
  stopCountdown()
  if (!checkpoint.value) return
  secondsLeft.value = Math.floor(checkpoint.value.timeout_sec)
  countdownTimer = setInterval(() => {
    secondsLeft.value -= 1
    if (secondsLeft.value <= 0) {
      stopCountdown()
      // The backend will time out the checkpoint; just close the dialog.
      errorMessage.value = 'Checkpoint timed out'
      visible.value = false
      pending.value = null
    }
  }, 1000)
}

function stopCountdown(): void {
  if (countdownTimer !== null) {
    clearInterval(countdownTimer)
    countdownTimer = null
  }
}

// --- Submission ----------------------------------------------------------

async function submitScanOrInput(): Promise<void> {
  if (!isValidInput() || !props.runId || !stepId.value) return
  await doSubmit(inputValue.value.trim(), null)
}

async function submitVisualCheck(passed: boolean): Promise<void> {
  if (!props.runId || !stepId.value) return
  if (!passed) {
    // First click on Fail -> show reason input; second click submits.
    if (!showFailReason.value) {
      showFailReason.value = true
      return
    }
    await doSubmit('fail', failReason.value.trim() || 'Operator rejected visual check')
    return
  }
  await doSubmit('pass', null)
}

async function submitConfirm(): Promise<void> {
  if (!props.runId || !stepId.value) return
  await doSubmit('ok', null)
}

async function doSubmit(response: string, reason: string | null): Promise<void> {
  if (!props.runId || !stepId.value) return
  submitting.value = true
  errorMessage.value = ''
  try {
    await http.post(`/executions/${props.runId}/checkpoint`, {
      step_id: stepId.value,
      response,
      reason,
      extra: {},
    })
    visible.value = false
    stopCountdown()
    pending.value = null
    if (response === 'fail' && reason) {
      emit('failed', stepId.value, reason)
    } else {
      emit('resolved', stepId.value, response)
    }
  } catch (err) {
    errorMessage.value = 'Failed to submit response. Please retry.'
    console.error('Checkpoint submission failed:', err)
  } finally {
    submitting.value = false
  }
}

function handleCancel(): void {
  if (stepId.value) emit('cancelled', stepId.value)
  visible.value = false
  stopCountdown()
  pending.value = null
}

// --- Watchers ------------------------------------------------------------

watch(
  () => props.runId,
  (newId) => {
    if (newId) {
      startPolling()
    } else {
      stopPolling()
      stopCountdown()
      visible.value = false
      pending.value = null
    }
  },
)

// --- Lifecycle -----------------------------------------------------------

onMounted(() => {
  if (props.runId) startPolling()
})

onBeforeUnmount(() => {
  stopPolling()
  stopCountdown()
})
</script>

<template>
  <ElDialog
    v-model="visible"
    :title="dialogTitle"
    width="480px"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    :show-close="false"
    @close="handleCancel"
  >
    <div v-if="checkpoint" class="checkpoint-body">
      <!-- Prompt -->
      <p class="checkpoint-prompt">{{ checkpoint.prompt }}</p>

      <!-- Timeout indicator -->
      <div class="timeout-bar">
        <ElTag :type="secondsLeft <= 10 ? 'danger' : 'info'" size="small">
          Timeout in {{ secondsLeft }}s
        </ElTag>
      </div>

      <!-- Scan / Manual input -->
      <div v-if="checkpointType === 'scan' || checkpointType === 'manual_input'" class="input-section">
        <ElInput
          v-model="inputValue"
          :placeholder="inputPlaceholder"
          :autofocus="checkpointType === 'scan'"
          :disabled="submitting"
          clearable
          @keyup.enter="submitScanOrInput"
        />
        <div v-if="errorMessage" class="error-text">{{ errorMessage }}</div>
      </div>

      <!-- Visual check -->
      <div v-else-if="checkpointType === 'visual_check'" class="visual-check-section">
        <div v-if="!showFailReason" class="check-buttons">
          <ElButton type="success" :loading="submitting" @click="submitVisualCheck(true)">
            Pass
          </ElButton>
          <ElButton type="danger" :loading="submitting" @click="submitVisualCheck(false)">
            Fail
          </ElButton>
        </div>
        <div v-else class="fail-reason-section">
          <ElInput
            v-model="failReason"
            placeholder="Reason for failure (optional)"
            :disabled="submitting"
            type="textarea"
            :rows="2"
          />
          <div class="fail-reason-buttons">
            <ElButton :disabled="submitting" @click="showFailReason = false">Back</ElButton>
            <ElButton type="danger" :loading="submitting" @click="submitVisualCheck(false)">
              Confirm Fail
            </ElButton>
          </div>
        </div>
      </div>

      <!-- Confirm -->
      <div v-else-if="checkpointType === 'confirm'" class="confirm-section">
        <ElButton type="primary" :loading="submitting" @click="submitConfirm">
          Confirm
        </ElButton>
      </div>
    </div>

    <template #footer>
      <div class="dialog-footer">
        <ElButton
          v-if="checkpointType === 'scan' || checkpointType === 'manual_input'"
          type="primary"
          :loading="submitting"
          :disabled="!isValidInput()"
          @click="submitScanOrInput"
        >
          Submit
        </ElButton>
      </div>
    </template>
  </ElDialog>
</template>

<style scoped>
.checkpoint-body {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md, 16px);
}

.checkpoint-prompt {
  font-size: 1rem;
  font-weight: 500;
  color: var(--color-text-primary, #1f2937);
  line-height: 1.5;
  margin: 0;
}

.timeout-bar {
  display: flex;
  justify-content: flex-end;
}

.input-section,
.visual-check-section,
.confirm-section {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm, 8px);
}

.check-buttons {
  display: flex;
  gap: var(--spacing-sm, 8px);
  justify-content: center;
}

.check-buttons .el-button {
  min-width: 120px;
}

.fail-reason-section {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm, 8px);
}

.fail-reason-buttons {
  display: flex;
  justify-content: flex-end;
  gap: var(--spacing-sm, 8px);
}

.confirm-section {
  align-items: center;
}

.confirm-section .el-button {
  min-width: 160px;
}

.error-text {
  color: var(--el-color-danger, #f56c6c);
  font-size: 0.875rem;
}

.dialog-footer {
  display: flex;
  justify-content: flex-end;
}

:deep(.el-dialog__header) {
  padding: var(--spacing-lg, 20px);
  border-bottom: 1px solid var(--color-border-default, #e5e7eb);
  margin: 0;
}

:deep(.el-dialog__title) {
  font-size: 1.125rem;
  font-weight: 600;
  color: var(--color-text-primary, #1f2937);
}

:deep(.el-dialog__body) {
  padding: var(--spacing-lg, 20px);
}
</style>
