import { computed, onBeforeUnmount, ref, shallowRef, watch, type Ref } from 'vue'
import { useEventSource } from '@vueuse/core'
import { ElMessage } from 'element-plus'
import {
  buildReplayStreamUrl,
  computeReplayDiff,
  listRecordings,
  pauseReplay,
  resumeReplay,
  startRecording,
  startReplay,
  type RecordedEventResponse,
  type ReplayDiffResponse,
  type ReplayResultResponse,
} from '@/api/simulation'

/**
 * Replay speed multiplier options exposed in the UI.
 */
export interface SpeedOption {
  value: number
  label: string
}

export const REPLAY_SPEEDS: readonly SpeedOption[] = [
  { value: 0.5, label: '0.5x' },
  { value: 1.0, label: '1x' },
  { value: 2.0, label: '2x' },
  { value: 5.0, label: '5x' },
] as const

/**
 * Recording lifecycle state.
 */
export type RecordingState = 'idle' | 'recording' | 'stopped' | 'error'

/**
 * Replay lifecycle state. The streaming replay supports an additional
 * 'paused' state via the pause/resume control endpoints.
 */
export type ReplayState = 'idle' | 'running' | 'paused' | 'completed' | 'error'

/**
 * A single SSE event delivered by the replay stream.
 * Mirrors RecordedEventResponse with a parsed `data` payload.
 */
export interface ReplayStreamEvent {
  id: string
  event: string
  data: RecordedEventResponse
}

/**
 * Composable for execution recording + replay control.
 *
 * Recording:
 * - `startRecording(runId)`: POST /executions/{runId}/record
 * - `recordingState`:        'idle' | 'recording' | 'stopped' | 'error'
 * - `recordedEvents`:        events captured from listRecordings() after stop
 *
 * Replay:
 * - `startReplayStream(runId)`: opens an SSE stream to /replay/stream
 * - `replayState`:              'idle' | 'running' | 'paused' | 'completed' | 'error'
 * - `replayEvents`:             accumulated events from the SSE stream
 * - `pauseReplay(runId)`:       POST /replay/pause
 * - `resumeReplay(runId)`:      POST /replay/resume
 * - `stopReplay()`:             closes the SSE stream (client-side stop)
 * - `speed`:                    selected speed multiplier (0.5/1/2/5)
 *
 * Diff:
 * - `computeDiff(runId, original)`: POST /replay/diff
 * - `diffResult`:                   last diff response or null
 *
 * The SSE stream is managed via @vueuse/core's useEventSource, mirroring the
 * pattern used in useExecutionStatus.ts. The stream URL is rebuilt whenever
 * `speed` changes; calling `stopReplay()` closes the connection.
 *
 * Errors are surfaced via ElMessage and the `error` ref - no silent fallback.
 *
 * @param activeRunId - Ref to the currently active execution run ID. When
 *   empty, recording/replay actions are no-ops.
 */
export function useReplay(activeRunId: Ref<string>) {
  // ── Recording state ──
  const recordingState = ref<RecordingState>('idle')
  const recordedEvents = ref<RecordedEventResponse[]>([])

  // ── Replay state ──
  const replayState = ref<ReplayState>('idle')
  const replayEvents = ref<RecordedEventResponse[]>([])
  const speed = ref<number>(1.0)
  const replayRunId = ref<string>('')

  // ── Diff state ──
  const diffResult = ref<ReplayDiffResponse | null>(null)

  // ── Shared error ──
  const error = ref<string | null>(null)
  const isStartingRecording = ref(false)
  const isStartingReplay = ref(false)

  // ── Speed options (for template v-for) ──
  const speeds = REPLAY_SPEEDS

  // ── Computed flags ──
  const isRecording = computed(() => recordingState.value === 'recording')
  const isReplaying = computed(
    () =>
      replayState.value === 'running' || replayState.value === 'paused',
  )
  const isPaused = computed(() => replayState.value === 'paused')

  /**
   * Build the SSE URL for the streaming replay. Empty when no replay is
   * active - useEventSource will not connect when the URL is empty.
   */
  const replayStreamUrl = computed(() =>
    replayRunId.value && replayState.value !== 'idle'
      ? buildReplayStreamUrl(replayRunId.value, speed.value)
      : '',
  )

  // ── SSE connection (mirrors useExecutionStatus pattern) ──
  // immediate: false so we control connect/disconnect via replayState changes.
  const { data: sseData, status: sseStatus, close: closeSse } = useEventSource(
    replayStreamUrl,
    [],
    {
      autoReconnect: {
        retries: 3,
        delay: 2000,
        onFailed() {
          replayState.value = 'error'
          error.value = 'Replay SSE connection failed after retries'
        },
      },
      immediate: false,
    },
  )

  // Track the current replay's run ID separately so we can detect when the
  // active execution changes mid-replay.
  const currentReplayRunId = shallowRef<string>('')

  // ── SSE event handler ──
  // Each SSE message carries a RecordedEventResponse JSON in `data`.
  watch(sseData, (raw) => {
    if (!raw) return
    let parsed: RecordedEventResponse
    try {
      parsed = JSON.parse(raw) as RecordedEventResponse
    } catch {
      // Malformed JSON - ignore partial frames
      return
    }
    replayEvents.value.push(parsed)
  })

  // ── SSE connection status watcher ──
  watch(sseStatus, (newStatus) => {
    if (newStatus === 'OPEN' && replayState.value === 'idle') {
      replayState.value = 'running'
    } else if (newStatus === 'CLOSED') {
      if (replayState.value === 'running' || replayState.value === 'paused') {
        // Stream ended naturally - mark as completed
        replayState.value = 'completed'
      }
    }
  })

  // ── Recording actions ──

  /**
   * Start recording events for the given execution.
   * POST /api/v1/executions/{runId}/record
   */
  async function startRecordingSession(runId: string): Promise<boolean> {
    if (!runId) {
      error.value = 'No run ID'
      ElMessage.warning('请先启动执行以获取 run ID')
      return false
    }
    isStartingRecording.value = true
    error.value = null
    try {
      const resp = await startRecording(runId, { auto_stop_on_complete: true })
      recordingState.value = 'recording'
      recordedEvents.value = []
      ElMessage.success(`录制已启动: ${resp.session_id}`)
      return true
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      error.value = msg
      recordingState.value = 'error'
      ElMessage.error(`录制启动失败: ${msg}`)
      return false
    } finally {
      isStartingRecording.value = false
    }
  }

  /**
   * Stop recording and fetch the captured events.
   * The backend auto-stops on EXECUTION_COMPLETED; this method fetches
   * the final event list via GET /executions/{runId}/recordings.
   */
  async function stopRecordingSession(runId: string): Promise<void> {
    if (!runId) return
    try {
      recordedEvents.value = await listRecordings(runId)
      recordingState.value = 'stopped'
      ElMessage.success(`录制已停止: ${recordedEvents.value.length} 个事件`)
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      error.value = msg
      recordingState.value = 'error'
      ElMessage.error(`停止录制失败: ${msg}`)
    }
  }

  // ── Replay actions ──

  /**
   * Start a streaming replay via SSE.
   *
   * Opens an EventSource to /api/v1/executions/{runId}/replay/stream?speed=N.
   * Events accumulate in `replayEvents`. Use `pauseReplaySession` /
   * `resumeReplaySession` to control the stream, and `stopReplay` to close.
   */
  async function startReplayStream(runId: string): Promise<boolean> {
    if (!runId) {
      error.value = 'No run ID'
      ElMessage.warning('请先启动执行以获取 run ID')
      return false
    }
    if (isReplaying.value) {
      ElMessage.warning('回放已在进行中')
      return false
    }

    isStartingReplay.value = true
    error.value = null
    replayEvents.value = []
    currentReplayRunId.value = runId
    replayRunId.value = runId
    // Setting replayState away from 'idle' makes replayStreamUrl non-empty,
    // which triggers useEventSource to connect.
    replayState.value = 'running'
    isStartingReplay.value = false
    ElMessage.success(`回放已启动 (速度 ${speed.value}x)`)
    return true
  }

  /**
   * Pause the active streaming replay.
   * POST /api/v1/executions/{runId}/replay/pause
   */
  async function pauseReplaySession(): Promise<void> {
    const runId = currentReplayRunId.value
    if (!runId || replayState.value !== 'running') return
    try {
      await pauseReplay(runId)
      replayState.value = 'paused'
      ElMessage.info('回放已暂停')
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      error.value = msg
      ElMessage.error(`暂停回放失败: ${msg}`)
    }
  }

  /**
   * Resume a paused streaming replay.
   * POST /api/v1/executions/{runId}/replay/resume
   */
  async function resumeReplaySession(): Promise<void> {
    const runId = currentReplayRunId.value
    if (!runId || replayState.value !== 'paused') return
    try {
      await resumeReplay(runId)
      replayState.value = 'running'
      ElMessage.info('回放已恢复')
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      error.value = msg
      ElMessage.error(`恢复回放失败: ${msg}`)
    }
  }

  /**
   * Stop the replay (client-side). Closes the SSE connection.
   * The backend replay executor will detect the disconnected client and
   * cancel itself.
   */
  function stopReplay(): void {
    closeSse()
    replayState.value = 'idle'
    replayRunId.value = ''
    currentReplayRunId.value = ''
  }

  /**
   * Run a synchronous (non-streaming) replay that returns all events at once.
   * Useful for the diff viewer when real-time streaming is not needed.
   * POST /api/v1/executions/{runId}/replay
   */
  async function runReplaySync(
    runId: string,
    maxEvents?: number,
  ): Promise<ReplayResultResponse | null> {
    if (!runId) {
      error.value = 'No run ID'
      return null
    }
    error.value = null
    try {
      const result = await startReplay(runId, {
        speed_multiplier: speed.value,
        max_events: maxEvents ?? null,
      })
      return result
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      error.value = msg
      ElMessage.error(`回放失败: ${msg}`)
      return null
    }
  }

  // ── Diff actions ──

  /**
   * Compute a diff between the original recording and the current replay.
   * POST /api/v1/executions/{runId}/replay/diff
   *
   * @param runId     The execution run ID.
   * @param original  The original recorded events (captured via stopRecordingSession).
   */
  async function computeDiff(
    runId: string,
    original: RecordedEventResponse[],
  ): Promise<ReplayDiffResponse | null> {
    if (!runId) {
      error.value = 'No run ID'
      return null
    }
    error.value = null
    try {
      const result = await computeReplayDiff(
        runId,
        original as unknown as Record<string, unknown>[],
      )
      diffResult.value = result
      return result
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      error.value = msg
      ElMessage.error(`Diff 计算失败: ${msg}`)
      return null
    }
  }

  // ── Reset ──

  /** Reset all recording + replay state. Does not close an active SSE stream. */
  function reset(): void {
    stopReplay()
    recordingState.value = 'idle'
    recordedEvents.value = []
    replayEvents.value = []
    diffResult.value = null
    error.value = null
  }

  // ── Cleanup ──
  onBeforeUnmount(() => {
    closeSse()
  })

  // If the active run ID changes, stop any active replay.
  watch(activeRunId, (newId, oldId) => {
    if (newId !== oldId && isReplaying.value) {
      stopReplay()
    }
  })

  return {
    // Recording state
    recordingState,
    recordedEvents,
    isRecording,
    isStartingRecording,
    // Replay state
    replayState,
    replayEvents,
    speed,
    isReplaying,
    isPaused,
    isStartingReplay,
    speeds,
    // Diff state
    diffResult,
    // Shared
    error,
    // Recording actions
    startRecordingSession,
    stopRecordingSession,
    // Replay actions
    startReplayStream,
    pauseReplaySession,
    resumeReplaySession,
    stopReplay,
    runReplaySync,
    // Diff actions
    computeDiff,
    // Reset
    reset,
  }
}
