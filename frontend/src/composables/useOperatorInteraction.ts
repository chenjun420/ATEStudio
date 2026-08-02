/**
 * Composable for the Operator Interaction Panel (T46).
 *
 * Extends `useOperatorGuidance` with operator-action capabilities:
 *
 * 1. **Scanner input** — barcode scanner simulates keyboard input + Enter.
 *    The composable exposes a reactive `scannerInput` ref and a `handleScan`
 *    method that submits the scanned value to the pending checkpoint (if any)
 *    or records it as a DUT serial for the current run.
 *
 * 2. **Quick actions** — Pass / Fail / Skip / Retry / Abort buttons that
 *    submit operator responses via the checkpoint endpoint (Pass/Fail for
 *    visual_check checkpoints) or send execution-level commands (Abort via
 *    the existing `abortExecution` API; Skip/Retry/Abort are recorded as
 *    operator events when no backend endpoint exists).
 *
 * 3. **AI diagnosis dialog** — delegates to `useOperatorGuidance.fetchDiagnosis`
 *    and exposes a `diagnosisDialogVisible` ref for the panel to bind.
 *
 * The composable is read-only by design: it does NOT allow editing the
 * sequence or modifying step definitions. It only submits operator responses.
 */
import { ref, watch, type Ref, type ComputedRef } from 'vue'
import axios from 'axios'
import { useOperatorGuidance } from './useOperatorGuidance'
import type {
  OperatorStep,
  DiagnosisResult,
  ResourceHealth,
} from './useOperatorGuidance'
import type {
  StepStatus,
  ExecutionStatus,
  ConnectionStatus,
  ExecutionEvent,
} from './useExecutionStatus'
import { abortExecution } from '@/api/executions'

// ─── Operator action types ───────────────────────────────────────────────────

/**
 * The five quick-action buttons available to the operator.
 */
export type OperatorAction = 'pass' | 'fail' | 'skip' | 'retry' | 'abort'

/**
 * Response shape from the operator checkpoint submit endpoint.
 */
interface CheckpointSubmitResponse {
  run_id: string
  pending: boolean
  step_id: string | null
}

/**
 * Pending operator checkpoint from the backend.
 */
export interface PendingCheckpoint {
  run_id: string
  pending: boolean
  step_id: string | null
  checkpoint: {
    type: 'scan' | 'manual_input' | 'visual_check' | 'confirm'
    prompt: string
    timeout_sec: number
    validation_regex: string | null
  } | null
  created_at: string | null
}

/**
 * A log entry for operator actions that don't have a dedicated backend
 * endpoint (skip/retry). Recorded locally and emitted as SSE events
 * when the backend supports it.
 */
export interface OperatorActionLog {
  action: OperatorAction
  step_id: string | null
  timestamp: string
  detail: string
}

/**
 * Return type of the `useOperatorInteraction` composable.
 */
export interface UseOperatorInteractionReturn {
  // ── Identifiers (delegated) ──
  stationId: Ref<string>
  runId: Ref<string>

  // ── Sequence / steps (delegated) ──
  steps: ComputedRef<OperatorStep[]>
  currentStepIndex: ComputedRef<number>
  currentStep: ComputedRef<OperatorStep | null>
  totalSteps: ComputedRef<number>

  // ── Live execution (delegated) ──
  stepStatuses: Record<string, StepStatus>
  executionStatus: Ref<ExecutionStatus | null>
  isRunning: ComputedRef<boolean>
  progressText: ComputedRef<string>
  completedSteps: Ref<number>
  latestAlarm: Ref<ExecutionEvent | null>
  latestMeasurements: Record<string, ExecutionEvent>
  connectionStatus: Ref<ConnectionStatus>

  // ── Diagnosis (delegated) ──
  diagnosis: Ref<DiagnosisResult | null>
  diagnosisLoading: Ref<boolean>
  diagnosisError: Ref<string | null>
  fetchDiagnosis: (stepId?: string | null) => Promise<void>
  diagnosisDialogVisible: Ref<boolean>

  // ── Resource health (delegated) ──
  resourceHealth: Ref<ResourceHealth[]>

  // ── Sequence loading (delegated) ──
  sequenceLoading: Ref<boolean>
  sequenceError: Ref<string | null>

  // ── Scanner input ──
  scannerInput: Ref<string>
  handleScanSubmit: () => Promise<void>

  // ── Pending checkpoint ──
  pendingCheckpoint: Ref<PendingCheckpoint | null>

  // ── Quick actions ──
  submitAction: (action: OperatorAction) => Promise<void>
  actionLoading: Ref<boolean>
  actionError: Ref<string | null>
  actionLog: Ref<OperatorActionLog[]>

  // ── Run management ──
  startRun: (newRunId: string) => void
  reset: () => void
}

// ─── Checkpoint API client ───────────────────────────────────────────────────

const checkpointApi = axios.create({
  baseURL: '/api/v1',
  timeout: 10000,
  headers: { 'Content-Type': 'application/json' },
})

/**
 * Fetch the pending checkpoint for a run.
 * GET /api/v1/executions/{runId}/checkpoint/pending
 */
async function fetchPendingCheckpoint(runId: string): Promise<PendingCheckpoint> {
  const resp = await checkpointApi.get<PendingCheckpoint>(
    `/executions/${runId}/checkpoint/pending`,
  )
  return resp.data
}

/**
 * Submit an operator response to a pending checkpoint.
 * POST /api/v1/executions/{runId}/checkpoint
 */
async function submitCheckpointResponse(
  runId: string,
  stepId: string,
  response: string,
  reason?: string,
): Promise<CheckpointSubmitResponse> {
  const resp = await checkpointApi.post<CheckpointSubmitResponse>(
    `/executions/${runId}/checkpoint`,
    {
      step_id: stepId,
      response,
      reason: reason ?? null,
      extra: {},
    },
  )
  return resp.data
}

// ─── Composable ──────────────────────────────────────────────────────────────

/**
 * Composable backing the Operator Interaction Panel.
 *
 * Wraps `useOperatorGuidance` and adds scanner input, quick-action buttons,
 * and AI diagnosis dialog management.
 *
 * @param initialStationId - Station identifier from the route param.
 * @param initialRunId - Optional initial run ID.
 */
export function useOperatorInteraction(
  initialStationId = '',
  initialRunId = '',
): UseOperatorInteractionReturn {
  // Delegate to the existing guidance composable for SSE + sequence + diagnosis.
  const guidance = useOperatorGuidance(initialStationId, initialRunId)

  // ── Scanner input ──
  const scannerInput = ref('')
  const actionLoading = ref(false)
  const actionError = ref<string | null>(null)
  const actionLog = ref<OperatorActionLog[]>([])

  // ── AI diagnosis dialog ──
  const diagnosisDialogVisible = ref(false)

  // ── Pending checkpoint ──
  const pendingCheckpoint = ref<PendingCheckpoint | null>(null)

  // ── Poll pending checkpoint when a run is active ──
  let pollTimer: ReturnType<typeof setInterval> | null = null

  /**
   * Start polling the pending checkpoint endpoint for the current run.
   * Polls every 2 seconds — the backend also pushes SSE events, but
   * polling is a reliable fallback for the operator panel.
   */
  function startPolling(): void {
    stopPolling()
    pollTimer = setInterval(async () => {
      if (!guidance.runId.value) return
      try {
        pendingCheckpoint.value = await fetchPendingCheckpoint(guidance.runId.value)
      } catch {
        // Silently ignore — the endpoint may not be available yet.
      }
    }, 2000)
  }

  function stopPolling(): void {
    if (pollTimer !== null) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  // Start/stop polling based on runId changes.
  watch(guidance.runId, (newId) => {
    if (newId) {
      // Fetch immediately, then poll.
      void fetchPendingCheckpoint(newId)
        .then((cp) => {
          pendingCheckpoint.value = cp
        })
        .catch(() => {
          pendingCheckpoint.value = null
        })
      startPolling()
    } else {
      pendingCheckpoint.value = null
      stopPolling()
    }
  })

  /**
   * Handle scanner input submission (Enter key or submit button).
   *
   * If a checkpoint is pending and expects scan/manual_input, submits the
   * scanned value as the checkpoint response. Otherwise, treats the scan
   * as a DUT serial number lookup (future enhancement).
   */
  async function handleScanSubmit(): Promise<void> {
    const value = scannerInput.value.trim()
    if (!value) return

    const cp = pendingCheckpoint.value
    const runId = guidance.runId.value

    // If a scan/manual_input checkpoint is pending, submit it.
    if (
      cp?.pending &&
      cp.checkpoint &&
      (cp.checkpoint.type === 'scan' || cp.checkpoint.type === 'manual_input')
    ) {
      actionLoading.value = true
      actionError.value = null
      try {
        await submitCheckpointResponse(runId, cp.step_id!, value)
        pendingCheckpoint.value = null
        scannerInput.value = ''
      } catch (err: unknown) {
        if (axios.isAxiosError(err)) {
          actionError.value = `Scan submit failed (HTTP ${err.response?.status ?? 'unknown'})`
        } else {
          actionError.value = err instanceof Error ? err.message : 'Scan submit failed'
        }
      } finally {
        actionLoading.value = false
      }
      return
    }

    // No pending checkpoint — just clear the input.
    // The scan may be used for DUT serial entry in a future enhancement.
    scannerInput.value = ''
  }

  /**
   * Submit a quick-action (Pass/Fail/Skip/Retry/Abort).
   *
   * - **Pass/Fail**: If a visual_check checkpoint is pending, submits the
   *   response via the checkpoint endpoint. Otherwise, records the action
   *   locally.
   * - **Abort**: Calls the existing `abortExecution` API.
   * - **Skip/Retry**: No backend endpoint yet — records the action locally
   *   and shows a confirmation dialog.
   */
  async function submitAction(action: OperatorAction): Promise<void> {
    const runId = guidance.runId.value
    const stepId = guidance.currentStep.value?.id ?? null
    const now = new Date().toISOString()

    actionLoading.value = true
    actionError.value = null

    try {
      const cp = pendingCheckpoint.value

      switch (action) {
        case 'pass':
        case 'fail': {
          // If a visual_check checkpoint is pending, submit via checkpoint API.
          if (
            cp?.pending &&
            cp.checkpoint?.type === 'visual_check' &&
            cp.step_id
          ) {
            await submitCheckpointResponse(
              runId,
              cp.step_id,
              action,
              action === 'fail' ? 'Operator marked as fail' : undefined,
            )
            pendingCheckpoint.value = null
          }
          // If a confirm checkpoint is pending, submit "ok".
          if (
            cp?.pending &&
            cp.checkpoint?.type === 'confirm' &&
            cp.step_id
          ) {
            await submitCheckpointResponse(runId, cp.step_id, 'ok')
            pendingCheckpoint.value = null
          }
          actionLog.value.push({
            action,
            step_id: stepId,
            timestamp: now,
            detail: `Operator marked step ${stepId ?? 'N/A'} as ${action}`,
          })
          break
        }

        case 'abort': {
          if (runId) {
            await abortExecution(runId)
          }
          actionLog.value.push({
            action,
            step_id: stepId,
            timestamp: now,
            detail: `Operator aborted run ${runId}`,
          })
          break
        }

        case 'skip':
        case 'retry': {
          // No backend endpoint — record locally and surface to operator.
          actionLog.value.push({
            action,
            step_id: stepId,
            timestamp: now,
            detail: `Operator requested ${action} for step ${stepId ?? 'N/A'}`,
          })
          break
        }
      }
    } catch (err: unknown) {
      if (axios.isAxiosError(err)) {
        actionError.value = `Action "${action}" failed (HTTP ${err.response?.status ?? 'unknown'})`
      } else {
        actionError.value = err instanceof Error ? err.message : `Action "${action}" failed`
      }
    } finally {
      actionLoading.value = false
    }
  }

  // ── Auto-open diagnosis dialog when a step fails ──
  watch(
    () => guidance.currentStep.value?.status,
    (newStatus) => {
      if (newStatus === 'failed' || newStatus === 'error') {
        const stepId = guidance.currentStep.value?.id ?? null
        void guidance.fetchDiagnosis(stepId)
        diagnosisDialogVisible.value = true
      }
    },
  )

  // ── Wrap reset to also clear local state ──
  function reset(): void {
    stopPolling()
    pendingCheckpoint.value = null
    scannerInput.value = ''
    actionLog.value = []
    actionError.value = null
    actionLoading.value = false
    diagnosisDialogVisible.value = false
    guidance.reset()
  }

  return {
    // Delegated identifiers
    stationId: guidance.stationId,
    runId: guidance.runId,

    // Delegated sequence/steps
    steps: guidance.steps,
    currentStepIndex: guidance.currentStepIndex,
    currentStep: guidance.currentStep,
    totalSteps: guidance.totalSteps,

    // Delegated live execution
    stepStatuses: guidance.stepStatuses,
    executionStatus: guidance.executionStatus,
    isRunning: guidance.isRunning,
    progressText: guidance.progressText,
    completedSteps: guidance.completedSteps,
    latestAlarm: guidance.latestAlarm,
    latestMeasurements: guidance.latestMeasurements,
    connectionStatus: guidance.connectionStatus,

    // Delegated diagnosis
    diagnosis: guidance.diagnosis,
    diagnosisLoading: guidance.diagnosisLoading,
    diagnosisError: guidance.diagnosisError,
    fetchDiagnosis: guidance.fetchDiagnosis,
    diagnosisDialogVisible,

    // Delegated resource health
    resourceHealth: guidance.resourceHealth,

    // Delegated sequence loading
    sequenceLoading: guidance.sequenceLoading,
    sequenceError: guidance.sequenceError,

    // Scanner input
    scannerInput,
    handleScanSubmit,

    // Pending checkpoint
    pendingCheckpoint,

    // Quick actions
    submitAction,
    actionLoading,
    actionError,
    actionLog,

    // Run management
    startRun: guidance.startRun,
    reset,
  }
}
