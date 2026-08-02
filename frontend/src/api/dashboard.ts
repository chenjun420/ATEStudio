import axios from 'axios'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 10000,
  headers: {
    'Content-Type': 'application/json',
  },
})

/**
 * Dashboard summary — aggregated overview.
 */
export interface DashboardSummary {
  active_workers: number
  total_executions_today: number
  completed_today: number
  failed_today: number
  pass_rate: number
  total_faults: number
}

/**
 * Station (worker) status entry.
 */
export interface StationStatus {
  worker_id: string
  hostname: string
  status: 'online' | 'offline'
  capabilities: string[]
  current_tasks: number
  max_concurrent_tasks: number
}

/**
 * Stations response.
 */
export interface StationsResponse {
  stations: StationStatus[]
  total: number
}

/**
 * Fault trend data point (hourly bucket).
 */
export interface FaultTrendPoint {
  hour: string
  count: number
}

/**
 * Top fault Pareto entry.
 */
export interface TopFault {
  category: string
  count: number
}

/**
 * Faults response — trend + Pareto.
 */
export interface FaultsResponse {
  trend: FaultTrendPoint[]
  top_faults: TopFault[]
}

/**
 * Recent execution summary.
 */
export interface RecentExecution {
  id: string
  status: string
  sequence_id: string | null
  started_at: string | null
  completed_at: string | null
}

/**
 * Executions response — today's breakdown.
 */
export interface ExecutionsResponse {
  total: number
  by_status: Record<string, number>
  recent: RecentExecution[]
}

/**
 * GET /api/v1/dashboard/summary — aggregated dashboard overview.
 */
export async function getSummary(): Promise<DashboardSummary> {
  const response = await api.get<DashboardSummary>('/dashboard/summary')
  return response.data
}

/**
 * GET /api/v1/dashboard/stations — per-station worker status.
 */
export async function getStations(): Promise<StationsResponse> {
  const response = await api.get<StationsResponse>('/dashboard/stations')
  return response.data
}

/**
 * GET /api/v1/dashboard/faults — fault trend + Top-5 Pareto.
 */
export async function getFaults(): Promise<FaultsResponse> {
  const response = await api.get<FaultsResponse>('/dashboard/faults')
  return response.data
}

/**
 * GET /api/v1/dashboard/executions — today's execution breakdown.
 */
export async function getExecutions(): Promise<ExecutionsResponse> {
  const response = await api.get<ExecutionsResponse>('/dashboard/executions')
  return response.data
}
