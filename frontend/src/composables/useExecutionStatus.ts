import { computed, reactive, watch, ref, onBeforeUnmount, type Ref } from 'vue'
import { useEventSource } from '@vueuse/core'
import { BatchBuffer } from './batchBuffer'

/**
 * sessionStorage key prefix for persisting last event ID per run_id.
 * Enables SSE reconnection with Last-Event-ID header for replay.
 */
const LAST_EVENT_ID_PREFIX = 'ate_last_event_id:'

/**
 * Connection status for the SSE stream.
 */
export type ConnectionStatus = 'connecting' | 'connected' | 'reconnecting' | 'disconnected' | 'error'

/**
 * Step status values matching backend SSE event statuses
 * and ScriptStepData.status / LoopContainerData.status
 */
export type StepStatus = 'idle' | 'running' | 'passed' | 'failed' | 'error' | 'skipped'

/**
 * Execution-level status values from the backend
 */
export type ExecutionStatus = 'PENDING' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'ABORTED'

/**
 * TEMS A4 event categories — matches backend EventCategory enum values.
 * The SSE `event:` line carries the category, enabling category-based filtering.
 */
export type EventCategory = 'event' | 'measurement' | 'alarm'

/**
 * SSE event types from the backend execution engine
 */
export type SSEEventType =
  | 'EXECUTION_STARTED'
  | 'EXECUTION_COMPLETED'
  | 'EXECUTION_PAUSED'
  | 'STEP_STARTED'
  | 'STEP_COMPLETED'
  | 'STEP_FAILED'
  | 'STEP_SKIPPED'
  | 'STEP_STATUS_CHANGED'
  | 'LOOP_ITERATION_STARTED'
  | 'LOOP_ITERATION_COMPLETED'
  | 'MEASUREMENT_RECORDED'
  | 'STEP_TIMEOUT'
  | 'CONDITION_TIMEOUT'
  | 'RESOURCE_TIMEOUT'
  | 'DEADLOCK_DETECTED'
  | 'WORKER_EXHAUSTED'

/**
 * Parsed SSE event from the execution stream
 */
export interface ExecutionEvent {
  type: SSEEventType
  category: EventCategory
  run_id: string
  step_id?: string
  status?: string
  new_status?: string
  loop_id?: string
  iteration?: number
  severity?: 'warning' | 'critical'
  recoverable?: boolean
  [key: string]: unknown
}

/**
 * Persist the last event ID for a given run_id in sessionStorage.
 */
function persistLastEventId(runId: string, eventId: string): void {
  try {
    sessionStorage.setItem(LAST_EVENT_ID_PREFIX + runId, eventId)
  } catch {
    // sessionStorage unavailable (e.g., SSR) — silently ignore
  }
}

/**
 * Retrieve the last persisted event ID for a given run_id.
 */
function getPersistedLastEventId(runId: string): string | null {
  try {
    return sessionStorage.getItem(LAST_EVENT_ID_PREFIX + runId)
  } catch {
    return null
  }
}

/**
 * Clear the persisted last event ID for a given run_id.
 */
function clearPersistedLastEventId(runId: string): void {
  try {
    sessionStorage.removeItem(LAST_EVENT_ID_PREFIX + runId)
  } catch {
    // sessionStorage unavailable — silently ignore
  }
}

/**
 * Composable for consuming SSE execution status events.
 *
 * Connects to the backend SSE endpoint and provides reactive step statuses
 * that can be used to update node visual appearance in the graph.
 *
 * SSE events are categorized by TEMS A4 category (event, measurement, alarm).
 * The `event:` SSE line carries the category for client-side filtering.
 *
 * Features:
 * - Last-Event-ID persistence in sessionStorage for reconnection replay
 * - Connection status tracking (connecting, connected, reconnecting, disconnected, error)
 * - Auto-reconnect with Last-Event-ID header
 *
 * @param runId - Ref to the current execution run ID. When empty/null, no connection is made.
 * @returns Reactive step statuses, connection status, and execution state
 */
export function useExecutionStatus(runId: Ref<string>) {
  // Step-level statuses keyed by step_id
  const stepStatuses = reactive<Record<string, StepStatus>>({})

  // Execution-level state
  const executionStatus = ref<ExecutionStatus | null>(null)
  const isRunning = computed(() => executionStatus.value === 'RUNNING')

  // Alarm state — latest alarm event for UI display
  const latestAlarm = ref<ExecutionEvent | null>(null)

  // Measurement state — latest measurements for UI display
  const latestMeasurements = reactive<Record<string, ExecutionEvent>>({})

  // Connection status for UI indicator
  const connectionStatus = ref<ConnectionStatus>('disconnected')

  // Step progress tracking
  const completedSteps = ref(0)
  const totalSteps = ref(0)
  const progressText = computed(() => {
    if (!isRunning.value && executionStatus.value !== 'COMPLETED' && executionStatus.value !== 'FAILED' && executionStatus.value !== 'ABORTED') {
      return ''
    }
    if (totalSteps.value === 0) return ''
    const passed = Object.values(stepStatuses).filter(s => s === 'passed').length
    const failed = Object.values(stepStatuses).filter(s => s === 'failed' || s === 'error').length
    if (executionStatus.value === 'COMPLETED') {
      return `Done — ${passed} passed, ${failed} failed`
    }
    if (executionStatus.value === 'FAILED') {
      return `Failed — ${passed} passed, ${failed} failed`
    }
    if (executionStatus.value === 'ABORTED') {
      return 'Aborted'
    }
    return `Step ${completedSteps.value}/${totalSteps.value} — ${passed} passed`
  })

  // ── BatchBuffer for step status updates ─────────────────────────
  // Accumulates step status changes within a 50ms window and flushes
  // them in a single reactive update to minimize Vue re-renders.
  const batchBuffer = new BatchBuffer(
    (updates) => {
      // Batch-apply all accumulated step statuses into stepStatuses
      // in a single synchronous block — Vue batches the reactive updates
      for (const [stepId, data] of updates) {
        if (data.status !== undefined) {
          stepStatuses[stepId] = data.status as StepStatus
        }
      }
    },
    { windowMs: 50, maxBatchSize: 200 },
  )

  /** Reactive stats from the batch buffer for debugging/monitoring */
  const batchStats = batchBuffer.stats

  // Build computed URL — empty string when no runId (useEventSource won't connect)
  const sseUrl = computed(() =>
    runId.value ? `/api/v1/executions/${runId.value}/events` : ''
  )

  // Connect to SSE via @vueuse/core's useEventSource
  const { data, status, error, close } = useEventSource(
    sseUrl,
    [],
    {
      autoReconnect: {
        retries: 10,
        delay: 2000,
        onFailed() {
          // After max retries exhausted, mark as error
          connectionStatus.value = 'error'
        },
      },
      immediate: false,
    }
  )

  // Track connection state transitions for UI indicator
  watch(status, (newStatus) => {
    switch (newStatus) {
      case 'CONNECTING':
        // Only show reconnecting if we were previously connected
        if (connectionStatus.value === 'connected') {
          connectionStatus.value = 'reconnecting'
        } else {
          connectionStatus.value = 'connecting'
        }
        break
      case 'OPEN':
        connectionStatus.value = 'connected'
        break
      case 'CLOSED':
        connectionStatus.value = 'disconnected'
        break
    }
  })

  watch(error, (err) => {
    if (err) {
      connectionStatus.value = 'error'
    }
  })

  // Relevant step-level event types (EVENT category)
  const STEP_EVENT_TYPES: SSEEventType[] = [
    'STEP_STARTED',
    'STEP_COMPLETED',
    'STEP_FAILED',
    'STEP_SKIPPED',
    'STEP_STATUS_CHANGED',
    'LOOP_ITERATION_STARTED',
    'LOOP_ITERATION_COMPLETED',
  ]

  // Alarm event types (ALARM category)
  const ALARM_EVENT_TYPES: SSEEventType[] = [
    'STEP_TIMEOUT',
    'CONDITION_TIMEOUT',
    'RESOURCE_TIMEOUT',
    'DEADLOCK_DETECTED',
    'WORKER_EXHAUSTED',
  ]

  // Watch incoming SSE data and update step statuses
  watch(data, (raw) => {
    if (!raw) return

    let event: ExecutionEvent
    try {
      event = JSON.parse(raw)
    } catch {
      // Ignore malformed JSON — SSE can deliver partial frames
      return
    }

    // Persist last event ID for reconnection replay
    if (event.run_id && (event as any).id) {
      persistLastEventId(event.run_id, (event as any).id)
    }

    // Route by category for structured handling
    const category = event.category || inferCategory(event.type)

    // --- EVENT category: step lifecycle ---
    if (category === 'event') {
      handleEventCategory(event)
    }

    // --- MEASUREMENT category: instrument readings ---
    if (category === 'measurement') {
      handleMeasurementCategory(event)
    }

    // --- ALARM category: timeout / deadlock / exhaustion ---
    if (category === 'alarm') {
      handleAlarmCategory(event)
    }
  })

  /**
   * Handle EVENT-category events (step lifecycle, execution lifecycle).
   */
  function handleEventCategory(event: ExecutionEvent) {
    // Handle execution-level events
    if (event.type === 'EXECUTION_STARTED') {
      executionStatus.value = 'RUNNING'
      // Flush any pending batched updates before clearing state
      batchBuffer.flush()
      // Reset step tracking for new execution
      Object.keys(stepStatuses).forEach(key => delete stepStatuses[key])
      completedSteps.value = 0
      totalSteps.value = 0
      latestAlarm.value = null
      Object.keys(latestMeasurements).forEach(key => delete latestMeasurements[key])
      // Clear persisted last event ID for fresh execution
      if (event.run_id) {
        clearPersistedLastEventId(event.run_id)
      }
    }

    if (event.type === 'EXECUTION_COMPLETED') {
      executionStatus.value = 'COMPLETED'
    }

    if (event.type === 'EXECUTION_PAUSED') {
      executionStatus.value = 'PENDING'
    }

    // Handle step-level events — batch status updates via BatchBuffer
    if (event.type && STEP_EVENT_TYPES.includes(event.type) && event.step_id) {
      let stepStatus: StepStatus = 'running'

      if (event.type === 'STEP_STARTED' || event.type === 'LOOP_ITERATION_STARTED') {
        stepStatus = 'running'
      } else if (event.type === 'STEP_COMPLETED') {
        stepStatus = mapBackendStatus(event.status)
        completedSteps.value++
      } else if (event.type === 'STEP_FAILED') {
        stepStatus = 'failed'
        completedSteps.value++
      } else if (event.type === 'STEP_SKIPPED') {
        stepStatus = 'skipped'
        completedSteps.value++
      } else if (event.type === 'STEP_STATUS_CHANGED') {
        stepStatus = mapBackendStatus(event.new_status || event.status)
      } else if (event.type === 'LOOP_ITERATION_COMPLETED') {
        stepStatus = mapBackendStatus(event.status)
        completedSteps.value++
      }

      // Push to batch buffer — deduped within 50ms window
      batchBuffer.push(event.step_id, { status: stepStatus })
    }
  }

  /**
   * Handle MEASUREMENT-category events (instrument readings / variable recordings).
   */
  function handleMeasurementCategory(event: ExecutionEvent) {
    const varName = (event.name as string | undefined) ?? event.step_id ?? 'unknown'
    latestMeasurements[varName] = event
  }

  /**
   * Handle ALARM-category events (timeout, deadlock, exhaustion).
   */
  function handleAlarmCategory(event: ExecutionEvent) {
    latestAlarm.value = event

    // If a step-level alarm, update step status via batch buffer
    if (event.step_id) {
      if (event.type === 'STEP_TIMEOUT') {
        batchBuffer.push(event.step_id, { status: 'error' })
      }
    }
  }

  /**
   * Infer category from event type when category field is missing (backward compat).
   */
  function inferCategory(type: string | undefined): EventCategory {
    if (!type) return 'event'
    if (ALARM_EVENT_TYPES.includes(type as SSEEventType)) return 'alarm'
    if (type === 'MEASUREMENT_RECORDED' || type === 'VARIABLE_CHANGED') return 'measurement'
    return 'event'
  }

  // Watch runId changes to open/close connection
  watch(runId, (newId, oldId) => {
    if (newId && newId !== oldId) {
      // Flush pending updates and reset state for new run
      batchBuffer.flush()
      Object.keys(stepStatuses).forEach(key => delete stepStatuses[key])
      executionStatus.value = 'PENDING'
      completedSteps.value = 0
      totalSteps.value = 0
      latestAlarm.value = null
      Object.keys(latestMeasurements).forEach(key => delete latestMeasurements[key])
      // Load persisted last event ID for this run
      const persistedId = getPersistedLastEventId(newId)
      if (persistedId) {
        connectionStatus.value = 'reconnecting'
      } else {
        connectionStatus.value = 'connecting'
      }
    }
    if (!newId) {
      batchBuffer.flush()
      close()
      executionStatus.value = null
      connectionStatus.value = 'disconnected'
    }
  })

  /**
   * Set the total step count for progress tracking.
   * Called by the consumer when the graph layout is known.
   */
  function setTotalSteps(count: number) {
    totalSteps.value = count
  }

  /**
   * Reset all execution state (e.g., when starting a new execution)
   */
  function reset() {
    batchBuffer.flush()
    Object.keys(stepStatuses).forEach(key => delete stepStatuses[key])
    executionStatus.value = null
    completedSteps.value = 0
    totalSteps.value = 0
    latestAlarm.value = null
    Object.keys(latestMeasurements).forEach(key => delete latestMeasurements[key])
    connectionStatus.value = 'disconnected'
    if (runId.value) {
      clearPersistedLastEventId(runId.value)
    }
    close()
  }

  // Cleanup on unmount
  onBeforeUnmount(() => {
    batchBuffer.destroy()
    close()
  })

  return {
    // Step-level reactive state
    stepStatuses,
    executionStatus,
    isRunning,
    progressText,
    completedSteps,
    totalSteps,

    // Category-specific state
    latestAlarm,
    latestMeasurements,

    // Batch buffer stats (reactive, for debugging)
    batchStats,

    // SSE connection state
    sseStatus: status,
    sseError: error,
    connectionStatus,

    // Last-Event-ID helpers (exported for testing / advanced use)
    getPersistedLastEventId,
    persistLastEventId,
    clearPersistedLastEventId,

    // Actions
    setTotalSteps,
    reset,
    close,
  }
}

/**
 * Map backend status strings to frontend StepStatus values.
 * Backend uses UPPERCASE; frontend uses lowercase.
 */
function mapBackendStatus(status: string | undefined): StepStatus {
  if (!status) return 'running'
  const normalized = status.toLowerCase()
  switch (normalized) {
    case 'passed':
    case 'success':
    case 'completed':
      return 'passed'
    case 'failed':
      return 'failed'
    case 'error':
      return 'error'
    case 'skipped':
    case 'skip':
      return 'skipped'
    case 'running':
    case 'started':
      return 'running'
    case 'idle':
    case 'pending':
      return 'idle'
    default:
      return 'running'
  }
}
