import http from './interceptor'
import type { DiffSummary } from '@/utils/diffView'
import type { StepControlPayload, StepMode } from '@/utils/stepModes'
import type { SimulationReportResponse } from '@/utils/simulationReportView'

const api = http

/** Response shape from POST /executions/{runId}/step-control (T40). */
export interface StepControlResponse {
  ok: boolean
  run_id: string
  mode: StepMode | string
  target_step_id: string | null
}

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

/**
 * ExecutionDiff summary envelope returned by the compare endpoint (T37).
 */
export type ExecutionDiffResponse = DiffSummary & {
  run_id: string
  baseline: string
}

/**
 * Compare a run against a baseline run.
 * GET /api/v1/executions/{runId}/diff?baseline={baseline}
 */
export async function fetchExecutionDiff(
  runId: string,
  baseline: string,
): Promise<ExecutionDiffResponse> {
  const response = await api.get<ExecutionDiffResponse>(`/executions/${runId}/diff`, {
    params: { baseline },
  })
  return response.data
}

/**
 * 手动故障注入载荷（T38，与后端 ManualFaultRequest 对齐）。
 */
export interface ManualFaultPayload {
  scope: 'link' | 'instrument' | 'step' | 'scheduler' | 'protocol'
  target_id: string
  fault_type: string
  params?: Record<string, unknown>
}

/**
 * 手动故障注入响应（T38，与后端 ManualFaultResponse 对齐）。
 */
export interface ManualFaultResponse {
  ok: boolean
  run_id: string
  scope: string
  layer: string
  target_id: string
  fault_type: string
  fault_id: string
}

/**
 * POST /api/v1/executions/{runId}/manual-fault - 手动故障注入（T38）。
 *
 * 将面板组合的故障规则转发给运行中执行的 FaultInjector（§7.7 运行时注入），
 * 禁止纯客户端模拟。
 */
export async function injectManualFault(
  runId: string,
  payload: ManualFaultPayload,
): Promise<ManualFaultResponse> {
  const response = await api.post<ManualFaultResponse>(
    `/executions/${runId}/manual-fault`,
    payload,
  )
  return response.data
}

/**
 * 类型化断点（T39，§8.4）：kind 决定匹配语义，condition 仅 condition 类型携带
 * （表达式仅服务端求值）。
 */
export interface TypedBreakpoint {
  id: string
  run_id: string
  kind: 'step' | 'instrument_call' | 'variable_change' | 'condition'
  target: string
  condition: string | null
  enabled: boolean
}

export interface TypedBreakpointPayload {
  kind: TypedBreakpoint['kind']
  target: string
  condition?: string
}

export interface BreakpointListResponse {
  items: TypedBreakpoint[]
  total: number
}

/**
 * POST /api/v1/executions/{runId}/breakpoints - 注册类型化断点（T39）。
 */
export async function createTypedBreakpoint(
  runId: string,
  payload: TypedBreakpointPayload,
): Promise<TypedBreakpoint> {
  const response = await api.post<TypedBreakpoint>(`/executions/${runId}/breakpoints`, payload)
  return response.data
}

/**
 * GET /api/v1/executions/{runId}/breakpoints - 运行的断点列表（T39）。
 */
export async function listTypedBreakpoints(runId: string): Promise<BreakpointListResponse> {
  const response = await api.get<BreakpointListResponse>(`/executions/${runId}/breakpoints`)
  return response.data
}

/**
 * DELETE /api/v1/executions/{runId}/breakpoints/{bpId} - 幂等删除断点（T39）。
 */
export async function deleteTypedBreakpoint(runId: string, bpId: string): Promise<void> {
  await api.delete(`/executions/${runId}/breakpoints/${bpId}`)
}

/**
 * POST /api/v1/executions/{runId}/resume - 恢复暂停的执行（断点命中后继续）。
 */
export async function resumeExecution(runId: string): Promise<{ id: string; status: string }> {
  const response = await api.post(`/executions/${runId}/resume`)
  return response.data
}

/**
 * POST /api/v1/executions/{runId}/step-control - 调试步进指令（T40，§8.4）。
 * 断点暂停后发送 over/into/out/run_to_cursor，边端调度器单步放行。
 */
export async function stepControlExecution(
  runId: string,
  payload: StepControlPayload,
): Promise<StepControlResponse> {
  const response = await api.post<StepControlResponse>(`/executions/${runId}/step-control`, payload)
  return response.data
}

export type { SimulationReportResponse }

/**
 * GET /api/v1/executions/{runId}/simulation-report - 仿真报告（T41）。
 *
 * 组合报告：覆盖率（T14）+ 资源竞争（T13）+ 故障记录；缺失数据按节降级
 * （available=false + warnings），中止/部分运行仍可渲染。
 */
export async function fetchSimulationReport(runId: string): Promise<SimulationReportResponse> {
  const response = await api.get<SimulationReportResponse>(`/executions/${runId}/simulation-report`)
  return response.data
}
