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
  error: string | null
  started_at: string | null
  completed_at: string | null
  created_at: string
  updated_at: string
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
