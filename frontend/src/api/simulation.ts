import http from './interceptor'

const api = http

// ─── Simulation tiers (D7 - 3-tier simulation) ──────────────────────────────

/**
 * Simulation tier - selects how deeply the execution is simulated.
 *
 * - `driver`:   Driver-level simulation - instruments replaced by SIM drivers,
 *               real scheduler + executor run.
 * - `dry_run`:  Dry-run scheduler - full scheduling graph traversal without
 *               real executors or instrument calls.
 * - `full`:     Full-chain simulation - end-to-end noise injection combining
 *               driver simulation with scheduler dry-run + noise model.
 */
export type SimulationTier = 'driver' | 'dry_run' | 'full'

/**
 * Noise model selector for full-chain simulation (only meaningful when
 * tier === 'full'). Mirrors backend NoiseModel enum values.
 *
 * - `GAUSSIAN`:        Pure Gaussian noise (sigma from config).
 * - `GAUSSIAN_DRIFT`:  Gaussian noise + linear time drift.
 * - `GAUSSIAN_BIAS`:   Gaussian noise + constant bias offset.
 * - `FULL`:            Gaussian + drift + bias (most realistic).
 */
export type NoiseModel = 'GAUSSIAN' | 'GAUSSIAN_DRIFT' | 'GAUSSIAN_BIAS' | 'FULL'

/**
 * Request body for POST /api/v1/executions/{id}/simulate.
 */
export interface SimulationRequest {
  /** Simulation tier to run. */
  tier: SimulationTier
  /** Noise model (only meaningful when tier === 'full'). */
  noise_model?: NoiseModel
  /** Gaussian noise sigma. Defaults to 0.001. */
  noise_sigma?: number
  /** Drift rate per second. Defaults to 0.0. */
  drift_rate?: number
  /** Constant bias offset. Defaults to 0.0. */
  bias?: number
  /** RNG seed for reproducibility. Defaults to 42. */
  seed?: number
  /** 故障注入规则列表（§7.7.2 fault_injection 段）。 */
  fault_config?: Array<Record<string, unknown>>
}

/**
 * A single simulated measurement result returned by the simulate endpoint.
 */
export interface SimulationResultEvent {
  step_id: string
  timestamp: string
  event_type: string
  data: Record<string, unknown>
}

/**
 * Response from POST /api/v1/executions/{id}/simulate.
 */
export interface SimulationResponse {
  session_id: string
  tier: SimulationTier
  status: string
  events: SimulationResultEvent[]
  duration_seconds: number
  statistics?: Record<string, number | string>
}

// ─── Recording (T26) ────────────────────────────────────────────────────────

/**
 * Request body for POST /api/v1/executions/{id}/record.
 */
export interface RecordStartRequest {
  auto_stop_on_complete?: boolean
}

/**
 * Response from POST /api/v1/executions/{id}/record.
 */
export interface RecordStartResponse {
  session_id: string
  subject: string
  status: string
  started_at: string
}

/**
 * Recording status from GET /api/v1/executions/{id}/recording.
 */
export interface RecordingStatusResponse {
  session_id: string
  is_recording: boolean
  event_count: number
  subject: string
}

// ─── Replay (T26) ───────────────────────────────────────────────────────────

/**
 * Request body for POST /api/v1/executions/{id}/replay.
 */
export interface ReplayStartRequest {
  speed_multiplier?: number
  max_events?: number | null
}

/**
 * A single recorded/replayed event.
 */
export interface RecordedEventResponse {
  timestamp: string
  event_type: string
  session_id: string
  step_id?: string | null
  data: Record<string, unknown>
}

/**
 * Response from POST /api/v1/executions/{id}/replay.
 */
export interface ReplayResultResponse {
  session_id: string
  status: string
  events_replayed: number
  events_total: number
  speed_multiplier: number
  duration_seconds: number
  events: RecordedEventResponse[]
}

/**
 * Replay control action response (pause/resume).
 */
export interface ReplayControlResponse {
  session_id: string
  action: string
  status: string
}

// ─── Diff (T26) ─────────────────────────────────────────────────────────────

export type ReplayDiffKind = 'added' | 'removed' | 'changed'

export interface ReplayDiffEntry {
  kind: ReplayDiffKind
  step_id: string
  event_type: string
  original: Record<string, unknown> | null
  replayed: Record<string, unknown> | null
}

export interface ReplayDiffSummary {
  original_count: number
  replayed_count: number
  added: number
  removed: number
  changed: number
}

export interface ReplayDiffResponse {
  session_id: string
  summary: ReplayDiffSummary
  entries: ReplayDiffEntry[]
}

// ─── API functions ──────────────────────────────────────────────────────────

/**
 * POST /api/v1/executions/{runId}/simulate - Run a 3-tier simulation.
 *
 * Driver-level / dry-run / full-chain simulation. The backend runs the
 * execution with the configured simulation tier and noise model, returning
 * the simulated events.
 */
export async function runSimulation(
  runId: string,
  data: SimulationRequest,
): Promise<SimulationResponse> {
  const response = await api.post<SimulationResponse>(
    `/executions/${runId}/simulate`,
    data,
  )
  return response.data
}

/**
 * POST /api/v1/executions/{runId}/record - Start recording execution events.
 *
 * Creates an ExecutionRecorder bound to the session and writes JSONL events
 * to the `ate.execution.{run_id}.events` JetStream subject.
 */
export async function startRecording(
  runId: string,
  data?: RecordStartRequest,
): Promise<RecordStartResponse> {
  const response = await api.post<RecordStartResponse>(
    `/executions/${runId}/record`,
    data ?? {},
  )
  return response.data
}

/**
 * GET /api/v1/executions/{runId}/recording - Get recording status.
 */
export async function getRecordingStatus(
  runId: string,
): Promise<RecordingStatusResponse> {
  const response = await api.get<RecordingStatusResponse>(
    `/executions/${runId}/recording`,
  )
  return response.data
}

/**
 * GET /api/v1/executions/{runId}/recordings - List recorded events.
 */
export async function listRecordings(
  runId: string,
): Promise<RecordedEventResponse[]> {
  const response = await api.get<RecordedEventResponse[]>(
    `/executions/${runId}/recordings`,
  )
  return response.data
}

/**
 * POST /api/v1/executions/{runId}/replay - Start replaying recorded events.
 *
 * Reads all recorded events, sorts them by timestamp, and returns them with
 * optional time acceleration. The replay is synchronous - the response
 * includes all replayed events.
 */
export async function startReplay(
  runId: string,
  data: ReplayStartRequest,
): Promise<ReplayResultResponse> {
  const response = await api.post<ReplayResultResponse>(
    `/executions/${runId}/replay`,
    data,
  )
  return response.data
}

/**
 * POST /api/v1/executions/{runId}/replay/pause - Pause an active streaming replay.
 */
export async function pauseReplay(
  runId: string,
): Promise<ReplayControlResponse> {
  const response = await api.post<ReplayControlResponse>(
    `/executions/${runId}/replay/pause`,
  )
  return response.data
}

/**
 * POST /api/v1/executions/{runId}/replay/resume - Resume a paused streaming replay.
 */
export async function resumeReplay(
  runId: string,
): Promise<ReplayControlResponse> {
  const response = await api.post<ReplayControlResponse>(
    `/executions/${runId}/replay/resume`,
  )
  return response.data
}

/**
 * POST /api/v1/executions/{runId}/replay/diff - Compute diff between two event sequences.
 *
 * Compares a caller-provided original event sequence with the events
 * currently recorded on the JetStream stream for this session.
 */
export async function computeReplayDiff(
  runId: string,
  originalEvents: Record<string, unknown>[],
): Promise<ReplayDiffResponse> {
  const response = await api.post<ReplayDiffResponse>(
    `/executions/${runId}/replay/diff`,
    originalEvents,
  )
  return response.data
}

/**
 * Build the SSE stream URL for a streaming replay.
 *
 * The backend exposes GET /api/v1/executions/{runId}/replay/stream?speed=N
 * which yields ServerSentEvent items with the recorded event type on the
 * `event:` line and the full event payload as JSON `data:`.
 */
export function buildReplayStreamUrl(runId: string, speed: number): string {
  return `/api/v1/executions/${runId}/replay/stream?speed=${speed}`
}
