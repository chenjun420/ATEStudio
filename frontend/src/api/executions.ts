import axios from 'axios'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 10000,
  headers: {
    'Content-Type': 'application/json',
  },
})

/**
 * Execution entity returned from the backend
 */
export interface Execution {
  id: string
  sequence_id: string | null
  status: string
  config: Record<string, unknown> | null
  result: Record<string, unknown> | null
  step_results: StepResult[] | null
  error: string | null
  started_at: string | null
  completed_at: string | null
  dut_serial: string | null
  station_id: string | null
  instrument_ids: string[] | null
  created_at: string
  updated_at: string
}

/**
 * Per-step result within an execution's step_results list.
 */
export interface StepResult {
  step_id: string
  name: string | null
  status: string
  started_at: string | null
  completed_at: string | null
  measurements?: MeasurementData[]
  error?: string | null
}

/**
 * Measurement data point within a step result.
 */
export interface MeasurementData {
  name: string
  value: number | null
  unit: string | null
  limits_min: number | null
  limits_max: number | null
  outcome: string
  timestamp: string | null
}

/**
 * Request body for creating a new execution
 */
export interface ExecutionCreate {
  sequence_id: string
  config?: Record<string, unknown> | null
}

/**
 * Response from the abort execution endpoint
 */
export interface ExecutionAbortResponse {
  id: string
  status: string
}

/**
 * Compact execution item for list/search table display.
 */
export interface ExecutionListItem {
  id: string
  sequence_id: string | null
  status: string
  dut_serial: string | null
  product_type: string | null
  started_at: string | null
  completed_at: string | null
  pass_rate: number | null
  error: string | null
}

/**
 * Request body for searching executions with advanced filters.
 */
export interface ExecutionSearchRequest {
  serial_number?: string
  product_type?: string
  status?: string
  date_from?: string
  date_to?: string
  skip?: number
  limit?: number
}

/**
 * Paginated response for execution search/list.
 */
export interface ExecutionSearchResponse {
  items: ExecutionListItem[]
  total: number
  skip: number
  limit: number
}

/**
 * Create a new execution for a sequence
 * POST /api/v1/executions
 */
export async function createExecution(data: ExecutionCreate): Promise<Execution> {
  const response = await api.post<Execution>('/executions', data)
  return response.data
}

/**
 * Get the status of an existing execution
 * GET /api/v1/executions/{runId}
 */
export async function getExecution(runId: string): Promise<Execution> {
  const response = await api.get<Execution>(`/executions/${runId}`)
  return response.data
}

/**
 * Abort a running execution
 * POST /api/v1/executions/{runId}/abort
 */
export async function abortExecution(runId: string): Promise<ExecutionAbortResponse> {
  const response = await api.post<ExecutionAbortResponse>(`/executions/${runId}/abort`)
  return response.data
}

/**
 * List executions with pagination.
 * GET /api/v1/executions?skip=0&limit=50
 */
export async function listExecutions(
  skip: number = 0,
  limit: number = 50,
): Promise<ExecutionSearchResponse> {
  const response = await api.get<ExecutionSearchResponse>('/executions', {
    params: { skip, limit },
  })
  return response.data
}

/**
 * Search executions with advanced filters.
 * POST /api/v1/executions/search
 */
export async function searchExecutions(
  params: ExecutionSearchRequest,
): Promise<ExecutionSearchResponse> {
  const response = await api.post<ExecutionSearchResponse>('/executions/search', params)
  return response.data
}
