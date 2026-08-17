/**
 * Composable for the Operator Guidance UI.
 *
 * Aggregates three data sources into a single reactive view-model for the
 * operator guidance panel:
 *
 * 1. **Sequence steps** - parsed from the execution's sequence YAML. Each step
 *    carries its id, script name, parameters, preconditions, expected resources,
 *    timeout, and retry policy - the "work instruction" the operator follows.
 * 2. **Live execution status** - delegated to the existing `useExecutionStatus`
 *    SSE composable. Provides per-step status (idle/running/passed/failed/...),
 *    execution-level status, progress text, the latest alarm, and the latest
 *    measurements.
 * 3. **AI diagnosis** - fetched on demand from the (forthcoming) diagnosis API
 *    endpoint `GET /api/v1/faults/diagnose`. When the backend is unreachable
 *    (T20 not yet deployed) the composable surfaces a clear "no diagnosis
 *    available" state rather than crashing - this is a UI affordance, not a
 *    backend silent-degradation.
 *
 * The composable is intentionally self-contained: it owns the runId ref so the
 * caller does not need to manage SSE lifecycle, and it watches the execution
 * status to auto-refresh the sequence metadata when a new run starts.
 */
import { ref, computed, watch, type Ref, type ComputedRef } from 'vue'
import * as yaml from 'js-yaml'
import axios from 'axios'
import { useExecutionStatus } from './useExecutionStatus'
import type { StepStatus } from './useExecutionStatus'
import type { YamlSequence, YamlStep, YamlLoop } from '@/types/dsl'
import { getExecution } from '@/api/executions'

// ─── Diagnosis domain types ──────────────────────────────────────────────────

/**
 * A single repair step suggested by the AI diagnosis engine.
 */
export interface RepairStep {
  /** 1-based ordinal for display ordering. */
  order: number
  /** Human-readable instruction, e.g. "Re-seat the DUT connector". */
  action: string
  /** Estimated time to perform the step, in seconds (optional). */
  estimated_seconds?: number
}

/**
 * A possible root cause returned by the diagnosis engine.
 */
export interface PossibleCause {
  /** Short label for the cause, e.g. "Loose power connector". */
  label: string
  /** Confidence score in [0, 1]. */
  confidence: number
  /** Optional longer description. */
  description?: string
}

/**
 * Full AI diagnosis result for a failed step or execution.
 *
 * Mirrors the shape the T20 backend will expose at
 * `GET /api/v1/faults/diagnose?run_id=...&step_id=...`.
 */
export interface DiagnosisResult {
  /** The step this diagnosis applies to, or null for execution-level. */
  step_id: string | null
  /** The most likely root cause. */
  root_cause: string
  /** Confidence in the root cause, [0, 1]. */
  confidence: number
  /** Ranked list of alternative possible causes. */
  possible_causes: PossibleCause[]
  /** Ordered repair suggestions the operator should follow. */
  repair_steps: RepairStep[]
  /** Free-form notes from the diagnosis engine. */
  notes?: string
}

/**
 * Health status of a single instrument or resource.
 */
export interface ResourceHealth {
  /** Resource identifier, e.g. "oscilloscope-1" or "dut-power-supply". */
  name: string
  /** Human-readable type, e.g. "Oscilloscope", "Power Supply". */
  type: string
  /** Current health: healthy, degraded, offline, unknown. */
  status: 'healthy' | 'degraded' | 'offline' | 'unknown'
  /** Optional detail message. */
  detail?: string
}

/**
 * Flattened step descriptor for the operator view.
 *
 * Derived from `YamlStep` but flattened and enriched with live status from
 * the SSE stream. Loops are unrolled into a single descriptor so the operator
 * sees a linear list of actionable steps.
 */
export interface OperatorStep {
  /** Step id from the YAML DSL. */
  id: string
  /** Script path / name to execute. */
  script: string
  /** Inline parameters passed to the script. */
  params: Record<string, unknown>
  /** Preconditions that must hold before the step runs. */
  preconditions: string[]
  /** Resource names the step declares (keys of the `resources` map). */
  resources: string[]
  /** Step timeout in milliseconds (DSL stores seconds; converted). */
  timeout_ms: number
  /** Retry count on failure. */
  retry: number
  /** On-fail policy: stop, skip, or ignore. */
  on_fail: string
  /** Live status from SSE, or 'idle' before the step runs. */
  status: StepStatus
}

/**
 * Return type of the `useOperatorGuidance` composable.
 */
export interface UseOperatorGuidanceReturn {
  // ── Identifiers ──
  stationId: Ref<string>
  runId: Ref<string>

  // ── Sequence / steps ──
  steps: ComputedRef<OperatorStep[]>
  currentStepIndex: ComputedRef<number>
  currentStep: ComputedRef<OperatorStep | null>
  totalSteps: ComputedRef<number>

  // ── Live execution (delegated to useExecutionStatus) ──
  stepStatuses: ReturnType<typeof useExecutionStatus>['stepStatuses']
  executionStatus: ReturnType<typeof useExecutionStatus>['executionStatus']
  isRunning: ReturnType<typeof useExecutionStatus>['isRunning']
  progressText: ReturnType<typeof useExecutionStatus>['progressText']
  completedSteps: ReturnType<typeof useExecutionStatus>['completedSteps']
  latestAlarm: ReturnType<typeof useExecutionStatus>['latestAlarm']
  latestMeasurements: ReturnType<typeof useExecutionStatus>['latestMeasurements']
  connectionStatus: ReturnType<typeof useExecutionStatus>['connectionStatus']

  // ── Diagnosis ──
  diagnosis: Ref<DiagnosisResult | null>
  diagnosisLoading: Ref<boolean>
  diagnosisError: Ref<string | null>
  fetchDiagnosis: (stepId?: string | null) => Promise<void>

  // ── Resource health ──
  resourceHealth: Ref<ResourceHealth[]>

  // ── Loading / error for sequence fetch ──
  sequenceLoading: Ref<boolean>
  sequenceError: Ref<string | null>

  // ── Actions ──
  startRun: (newRunId: string) => void
  reset: () => void
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

/**
 * Type guard distinguishing a YamlStep from a YamlLoop.
 * Loops have `loop_type`; steps have `script`.
 */
function isYamlStep(node: YamlStep | YamlLoop): node is YamlStep {
  return 'script' in node
}

/**
 * Recursively flatten a YamlSequence's steps (including nested loops) into a
 * flat list of OperatorStep descriptors. Loops contribute their child steps;
 * the loop itself is not a runnable step for the operator.
 *
 * @param nodes - Top-level step/loop nodes from the parsed sequence.
 * @param out - Accumulator (mutated in place).
 */
function flattenSteps(
  nodes: Array<YamlStep | YamlLoop>,
  out: OperatorStep[] = [],
): OperatorStep[] {
  for (const node of nodes) {
    if (isYamlStep(node)) {
      out.push({
        id: node.id,
        script: node.script ?? '',
        params: node.params ?? {},
        preconditions: node.preconditions ?? [],
        resources: node.resources ? Object.keys(node.resources) : [],
        timeout_ms: node.timeout ? Math.ceil(node.timeout * 1000) : 60000,
        retry: node.retry ?? 0,
        on_fail: node.on_fail ?? 'stop',
        status: 'idle',
      })
    } else {
      // YamlLoop - recurse into its children
      if (node.steps) {
        flattenSteps(node.steps, out)
      }
    }
  }
  return out
}

/**
 * Parse a sequence YAML string into a flat list of operator steps.
 * Throws on invalid YAML or missing steps array.
 */
function parseSequenceSteps(yamlContent: string): OperatorStep[] {
  const parsed = yaml.load(yamlContent) as YamlSequence
  if (!parsed || typeof parsed !== 'object') {
    throw new Error('Invalid YAML: must be an object')
  }
  if (!parsed.steps || !Array.isArray(parsed.steps)) {
    throw new Error('Invalid YAML: missing or invalid steps array')
  }
  return flattenSteps(parsed.steps)
}

/**
 * Find the index of the first step that is currently running, or failing that,
 * the first step that has not yet passed. Returns 0 if all steps are idle.
 */
function findCurrentStepIndex(
  steps: OperatorStep[],
  statuses: Record<string, StepStatus>,
): number {
  // Prefer a running step
  for (let i = 0; i < steps.length; i++) {
    if (statuses[steps[i]!.id] === 'running') return i
  }
  // Then the first non-passed, non-skipped step
  for (let i = 0; i < steps.length; i++) {
    const s = statuses[steps[i]!.id]
    if (s !== 'passed' && s !== 'skipped') return i
  }
  // All passed/skipped - point past the last step
  return steps.length
}

// ─── Diagnosis API client ────────────────────────────────────────────────────

const diagnosisApi = axios.create({
  baseURL: '/api/v1',
  timeout: 15000,
  headers: { 'Content-Type': 'application/json' },
})

/**
 * Fetch a diagnosis for a given run (and optionally a specific step).
 *
 * Calls `GET /api/v1/faults/diagnose?run_id=...&step_id=...`.
 * This endpoint is provided by T20 (not yet deployed). When the endpoint is
 * unavailable, the error is surfaced to the caller - no silent fallback.
 */
async function fetchDiagnosisFromApi(
  runId: string,
  stepId: string | null,
): Promise<DiagnosisResult> {
  const params: Record<string, string> = { run_id: runId }
  if (stepId) params.step_id = stepId
  const resp = await diagnosisApi.get<DiagnosisResult>('/faults/diagnose', { params })
  return resp.data
}

// ─── Composable ──────────────────────────────────────────────────────────────

/**
 * Composable backing the Operator Guidance view.
 *
 * @param initialStationId - Optional initial station identifier (from route param).
 * @param initialRunId - Optional initial run ID to connect SSE immediately.
 */
export function useOperatorGuidance(
  initialStationId = '',
  initialRunId = '',
): UseOperatorGuidanceReturn {
  const stationId = ref(initialStationId)
  const runId = ref(initialRunId)

  // Delegate SSE consumption to the existing composable.
  const sse = useExecutionStatus(runId)

  // ── Parsed sequence steps (static metadata from YAML) ──
  const rawSteps = ref<OperatorStep[]>([])
  const sequenceLoading = ref(false)
  const sequenceError = ref<string | null>(null)

  /**
   * Steps enriched with live status from the SSE stream.
   * Recomputed whenever rawSteps or stepStatuses change.
   */
  const steps = computed<OperatorStep[]>(() => {
    return rawSteps.value.map((s) => ({
      ...s,
      status: sse.stepStatuses[s.id] ?? 'idle',
    }))
  })

  const currentStepIndex = computed(() =>
    findCurrentStepIndex(steps.value, sse.stepStatuses),
  )
  const currentStep = computed(() =>
    steps.value[currentStepIndex.value] ?? null,
  )
  const totalSteps = computed(() => steps.value.length)

  // ── Diagnosis state ──
  const diagnosis = ref<DiagnosisResult | null>(null)
  const diagnosisLoading = ref(false)
  const diagnosisError = ref<string | null>(null)

  /**
   * Fetch an AI diagnosis for the current run and (optionally) a specific step.
   * Clears any previous diagnosis and error before loading.
   */
  async function fetchDiagnosis(stepId: string | null = null): Promise<void> {
    if (!runId.value) {
      diagnosisError.value = 'No active run to diagnose.'
      return
    }
    diagnosisLoading.value = true
    diagnosisError.value = null
    diagnosis.value = null
    try {
      diagnosis.value = await fetchDiagnosisFromApi(runId.value, stepId)
    } catch (err: unknown) {
      // T20 backend not yet deployed - surface the error to the UI.
      // This is a UI affordance (operator sees "no diagnosis"), NOT a
      // backend silent-degradation: the backend itself never fakes success.
      if (axios.isAxiosError(err)) {
        const code = err.response?.status
        if (code === 404) {
          diagnosisError.value = 'Diagnosis endpoint not available yet (T20 pending).'
        } else if (code) {
          diagnosisError.value = `Diagnosis request failed (HTTP ${code}).`
        } else {
          diagnosisError.value = 'Diagnosis service unreachable.'
        }
      } else {
        diagnosisError.value = err instanceof Error ? err.message : 'Unknown diagnosis error.'
      }
    } finally {
      diagnosisLoading.value = false
    }
  }

  // ── Resource health (instrument statuses) ──
  // Derived from the steps' declared resources + the latest alarm. When a
  // RESOURCE_TIMEOUT alarm fires, the implicated resources are marked degraded.
  const resourceHealth = ref<ResourceHealth[]>([])

  /**
   * Rebuild the resource health list whenever the parsed steps change.
   * Each declared resource starts as 'unknown' until the backend reports status.
   */
  function rebuildResourceHealth(): void {
    const seen = new Map<string, ResourceHealth>()
    for (const step of rawSteps.value) {
      for (const r of step.resources) {
        if (!seen.has(r)) {
          seen.set(r, {
            name: r,
            type: r.split('-')[0] ?? 'resource',
            status: 'unknown',
          })
        }
      }
    }
    resourceHealth.value = Array.from(seen.values())
  }

  // When steps change, rebuild the resource list.
  watch(rawSteps, rebuildResourceHealth, { immediate: true })

  // When a RESOURCE_TIMEOUT alarm fires, mark implicated resources as degraded.
  watch(sse.latestAlarm, (alarm) => {
    if (!alarm) return
    if (alarm.type === 'RESOURCE_TIMEOUT' || alarm.type === 'DEADLOCK_DETECTED') {
      const resourceName = (alarm.resource as string | undefined) ?? (alarm.step_id as string | undefined)
      if (resourceName) {
        const existing = resourceHealth.value.find((r) => r.name === resourceName)
        if (existing) {
          existing.status = 'degraded'
          existing.detail = alarm.type
        }
      }
    }
  })

  // ── Sequence loading ──

  /**
   * Load the sequence YAML for a given execution, parse it, and populate
   * rawSteps. Called automatically when runId changes.
   */
  async function loadSequenceForRun(id: string): Promise<void> {
    if (!id) {
      rawSteps.value = []
      return
    }
    sequenceLoading.value = true
    sequenceError.value = null
    try {
      const execution = await getExecution(id)
      if (execution.sequence_id) {
        // Lazy-import to avoid circular dependency at module load.
        const { fetchSequenceById } = await import('@/api/sequences')
        const seq = await fetchSequenceById(execution.sequence_id)
        if (seq.yaml_content) {
          rawSteps.value = parseSequenceSteps(seq.yaml_content)
          // Inform the SSE composable of the total step count for progress.
          sse.setTotalSteps(rawSteps.value.length)
        }
      }
    } catch (err: unknown) {
      sequenceError.value =
        err instanceof Error ? err.message : 'Failed to load sequence.'
      rawSteps.value = []
    } finally {
      sequenceLoading.value = false
    }
  }

  // Auto-load sequence when runId changes (new execution started).
  watch(runId, (newId) => {
    if (newId) {
      void loadSequenceForRun(newId)
    } else {
      rawSteps.value = []
    }
  })

  // ── Actions ──

  /**
   * Start tracking a new execution run. Sets the runId (which opens the SSE
   * connection via useExecutionStatus) and triggers sequence loading.
   */
  function startRun(newRunId: string): void {
    sse.reset()
    runId.value = newRunId
  }

  /**
   * Reset all state - disconnects SSE, clears steps, diagnosis, and resources.
   */
  function reset(): void {
    sse.reset()
    runId.value = ''
    rawSteps.value = []
    diagnosis.value = null
    diagnosisError.value = null
    diagnosisLoading.value = false
    resourceHealth.value = []
    sequenceError.value = null
  }

  return {
    stationId,
    runId,

    steps,
    currentStepIndex,
    currentStep,
    totalSteps,

    stepStatuses: sse.stepStatuses,
    executionStatus: sse.executionStatus,
    isRunning: sse.isRunning,
    progressText: sse.progressText,
    completedSteps: sse.completedSteps,
    latestAlarm: sse.latestAlarm,
    latestMeasurements: sse.latestMeasurements,
    connectionStatus: sse.connectionStatus,

    diagnosis,
    diagnosisLoading,
    diagnosisError,
    fetchDiagnosis,

    resourceHealth,

    sequenceLoading,
    sequenceError,

    startRun,
    reset,
  }
}

// Re-export the delegated types for consumer convenience.
export type {
  StepStatus,
  ExecutionStatus,
  ConnectionStatus,
  ExecutionEvent,
} from './useExecutionStatus'
