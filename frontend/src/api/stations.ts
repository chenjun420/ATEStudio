import axios from 'axios'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 10000,
  headers: {
    'Content-Type': 'application/json',
  },
})

/**
 * Worker metadata returned by GET /api/v1/workers.
 */
export interface WorkerInfo {
  worker_id: string
  hostname: string
  capabilities: string[]
  max_concurrent_tasks: number
  current_tasks: number
  last_heartbeat: string | null
}

/**
 * Response from GET /api/v1/workers — list of all registered workers.
 */
export interface WorkerListResponse {
  workers: WorkerInfo[]
  total: number
}

/**
 * Worker health status returned by GET /api/v1/workers/{id}/health.
 */
export interface WorkerHealthResponse {
  status: 'online' | 'offline' | 'unknown'
  worker_info: WorkerInfo | null
  last_heartbeat_timestamp: string | null
}

/**
 * A single heartbeat history record from GET /api/v1/workers/{id}/history.
 */
export interface HeartbeatRecord {
  id: string
  worker_id: string
  hostname: string
  status: string
  capabilities: string[]
  current_tasks: number
  recorded_at: string
  created_at: string
}

/**
 * Response from GET /api/v1/workers/{id}/history.
 */
export interface HeartbeatHistoryResponse {
  items: HeartbeatRecord[]
  total: number
}

/**
 * Worker version info (from sync/tag operations).
 */
export interface WorkerVersionInfo {
  script_path: string
  commit_hash: string
  revision: number
  tagged_at: string | null
}

/**
 * Response from POST /api/v1/workers/{id}/sync.
 */
export interface WorkerSyncResponse {
  synced: WorkerVersionInfo[]
  failed: string[]
}

/**
 * Worker configuration key-value pair from GET /api/v1/workers/{id}/config.
 */
export interface WorkerConfigEntry {
  key: string
  value: string
}

/**
 * Response from GET /api/v1/workers/{id}/config — all config keys.
 */
export interface WorkerConfigResponse {
  worker_id: string
  configs: WorkerConfigEntry[]
}

/**
 * GET /api/v1/workers — list all registered workers.
 */
export async function getWorkers(): Promise<WorkerListResponse> {
  const response = await api.get<WorkerListResponse>('/workers')
  return response.data
}

/**
 * GET /api/v1/workers/{workerId} — get a single worker's metadata.
 */
export async function getWorker(workerId: string): Promise<WorkerInfo> {
  const response = await api.get<WorkerInfo>(`/workers/${workerId}`)
  return response.data
}

/**
 * GET /api/v1/workers/{workerId}/health — get worker health status.
 */
export async function getWorkerHealth(workerId: string): Promise<WorkerHealthResponse> {
  const response = await api.get<WorkerHealthResponse>(`/workers/${workerId}/health`)
  return response.data
}

/**
 * GET /api/v1/workers/{workerId}/history — get heartbeat time-series.
 */
export async function getWorkerHistory(
  workerId: string,
  limit = 100,
): Promise<HeartbeatHistoryResponse> {
  const response = await api.get<HeartbeatHistoryResponse>(
    `/workers/${workerId}/history`,
    { params: { limit } },
  )
  return response.data
}

/**
 * GET /api/v1/workers/{workerId}/config — get all config entries for a worker.
 */
export async function getWorkerConfig(workerId: string): Promise<WorkerConfigResponse> {
  const response = await api.get<WorkerConfigResponse>(`/workers/${workerId}/config`)
  return response.data
}

/**
 * PUT /api/v1/workers/{workerId}/config/{key} — update a single config key.
 */
export async function updateWorkerConfig(
  workerId: string,
  key: string,
  value: string,
): Promise<{ revision: number }> {
  const response = await api.put<{ revision: number }>(
    `/workers/${workerId}/config/${key}`,
    { value },
  )
  return response.data
}

/**
 * POST /api/v1/workers/{workerId}/sync — trigger version sync for a worker.
 */
export async function syncWorker(workerId: string): Promise<WorkerSyncResponse> {
  const response = await api.post<WorkerSyncResponse>(`/workers/${workerId}/sync`)
  return response.data
}

/**
 * POST /api/v1/workers/{workerId}/restart — trigger a worker restart.
 * Falls back to sync endpoint if restart endpoint is not available.
 */
export async function restartWorker(workerId: string): Promise<WorkerSyncResponse> {
  const response = await api.post<WorkerSyncResponse>(`/workers/${workerId}/sync`)
  return response.data
}
