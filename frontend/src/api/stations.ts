import http from './interceptor'

const api = http

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
 * Response from POST /api/v1/workers/{workerId}/restart.
 */
export interface WorkerRestartResponse {
  status: string
  worker_id: string
}

/**
 * POST /api/v1/workers/{workerId}/restart — trigger a worker restart.
 *
 * Publishes a restart control message to the worker via NATS on
 * ``ate.control.{workerId}`. The worker's control subscription
 * handles the restart action.
 */
export async function restartWorker(workerId: string): Promise<WorkerRestartResponse> {
  const response = await api.post<WorkerRestartResponse>(`/workers/${workerId}/restart`)
  return response.data
}

// ─── Node Registration ──────────────────────────────────────────────────────

/**
 * Payload for registering a new worker node.
 */
export interface WorkerRegisterRequest {
  worker_id: string
  hostname: string
  capabilities?: string[]
  max_concurrent_tasks?: number
}

/**
 * POST /api/v1/workers — manually register a node.
 *
 * Writes a heartbeat entry to the ``ate-workers`` KV bucket so that a
 * worker appears in the registry without having sent its own heartbeat
 * yet. The per-key TTL (30s) still applies — the worker must take over
 * heartbeating to stay visible.
 */
export async function registerWorker(data: WorkerRegisterRequest): Promise<WorkerInfo> {
  const response = await api.post<WorkerInfo>('/workers', data)
  return response.data
}

/**
 * DELETE /api/v1/workers/{workerId} — delete a registered node.
 *
 * Removes the worker's heartbeat key from the ``ate-workers`` KV bucket.
 */
export async function deleteWorker(workerId: string): Promise<void> {
  await api.delete(`/workers/${workerId}`)
}

// ─── Node-Flow Bindings ─────────────────────────────────────────────────────

/**
 * Node-flow binding returned from the backend.
 */
export interface NodeFlowBinding {
  id: string
  worker_id: string
  sequence_id: string
  sequence_name: string | null
  is_active: boolean
  priority: number
  config: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

/**
 * Payload for creating a new node-flow binding.
 */
export interface NodeFlowBindingCreate {
  worker_id: string
  sequence_id: string
  is_active?: boolean
  priority?: number
  config?: Record<string, unknown> | null
}

/**
 * Payload for updating an existing binding.
 */
export interface NodeFlowBindingUpdate {
  is_active?: boolean
  priority?: number
  config?: Record<string, unknown> | null
}

/**
 * List response for node-flow bindings.
 */
export interface NodeFlowBindingListResponse {
  items: NodeFlowBinding[]
  total: number
}

/**
 * Response from triggering execution from a binding.
 */
export interface BindingExecuteResponse {
  execution_id: string
  status: string
}

/**
 * POST /api/v1/node-flow-bindings — create a new binding.
 */
export async function createNodeFlowBinding(data: NodeFlowBindingCreate): Promise<NodeFlowBinding> {
  const response = await api.post<NodeFlowBinding>('/node-flow-bindings', data)
  return response.data
}

/**
 * GET /api/v1/node-flow-bindings — list all bindings.
 */
export async function listNodeFlowBindings(skip = 0, limit = 100): Promise<NodeFlowBindingListResponse> {
  const response = await api.get<NodeFlowBindingListResponse>('/node-flow-bindings', {
    params: { skip, limit },
  })
  return response.data
}

/**
 * GET /api/v1/node-flow-bindings/by-worker/{workerId} — list bindings for a worker.
 */
export async function listBindingsByWorker(workerId: string): Promise<NodeFlowBindingListResponse> {
  const response = await api.get<NodeFlowBindingListResponse>(`/node-flow-bindings/by-worker/${workerId}`)
  return response.data
}

/**
 * PUT /api/v1/node-flow-bindings/{bindingId} — update a binding.
 */
export async function updateNodeFlowBinding(bindingId: string, data: NodeFlowBindingUpdate): Promise<NodeFlowBinding> {
  const response = await api.put<NodeFlowBinding>(`/node-flow-bindings/${bindingId}`, data)
  return response.data
}

/**
 * DELETE /api/v1/node-flow-bindings/{bindingId} — delete a binding.
 */
export async function deleteNodeFlowBinding(bindingId: string): Promise<void> {
  await api.delete(`/node-flow-bindings/${bindingId}`)
}

/**
 * POST /api/v1/node-flow-bindings/{bindingId}/execute — trigger execution from a binding.
 */
export async function executeBinding(bindingId: string): Promise<BindingExecuteResponse> {
  const response = await api.post<BindingExecuteResponse>(`/node-flow-bindings/${bindingId}/execute`)
  return response.data
}
